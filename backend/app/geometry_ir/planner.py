from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.validation.verification.evaluator import (
    build_verification_targets,
    summarize_verification_targets,
)

from .contracts import (
    Axis,
    Feature,
    FeatureRef,
    FeatureType,
    GeometryPlan,
    Parameter,
    ParameterSource,
    Sketch,
    SketchEntity,
    SketchEntityType,
)
from .identity import bounded_identifier, feature_candidates, slug_identifier, unique_identifier


def _mapping_view(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="python")
    raise TypeError(f"unsupported plan-like object: {type(value)!r}")


def _slugify(value: str) -> str:
    return slug_identifier(value, fallback="item")


def _feature_type_from_text(value: str) -> FeatureType:
    lowered = value.casefold()
    if any(token in lowered for token in ("hole", "drill", "bore", "counterbore")):
        return FeatureType.HOLE
    if any(token in lowered for token in ("fillet", "round")):
        return FeatureType.FILLET
    if any(token in lowered for token in ("chamfer", "bevel")):
        return FeatureType.CHAMFER
    if any(token in lowered for token in ("shell", "thin wall")):
        return FeatureType.SHELL
    if any(token in lowered for token in ("pattern", "array")):
        return FeatureType.PATTERN
    if any(token in lowered for token in ("mirror",)):
        return FeatureType.MIRROR
    if any(token in lowered for token in ("draft", "taper")):
        return FeatureType.DRAFT
    if any(token in lowered for token in ("rib", "gusset")):
        return FeatureType.RIB
    if any(token in lowered for token in ("revolve", "revolution")):
        return FeatureType.REVOLVE
    if any(token in lowered for token in ("sweep",)):
        return FeatureType.SWEEP
    if any(token in lowered for token in ("loft",)):
        return FeatureType.LOFT
    if any(token in lowered for token in ("extrude", "extrusion", "boss", "pad", "cut")):
        return FeatureType.EXTRUDE
    if any(token in lowered for token in ("boolean", "union", "difference")):
        return FeatureType.BOOLEAN
    if any(token in lowered for token in ("sketch", "profile")):
        return FeatureType.SKETCH
    return FeatureType.CUSTOM


def _primary_feature_name(plan_data: Mapping[str, Any]) -> str:
    features = [str(item).strip() for item in plan_data.get("features") or () if str(item).strip()]
    if features:
        return features[0]
    steps = plan_data.get("steps") or ()
    for step in steps:
        step_data = _mapping_view(step)
        description = str(step_data.get("description") or "").strip()
        if description:
            return description
    objective = str(plan_data.get("objective") or plan_data.get("description") or "").strip()
    return objective or "main body"


def _build_parameters(plan_data: Mapping[str, Any]) -> tuple[Parameter, ...]:
    parameters: list[Parameter] = []
    seen: set[str] = set()

    def add_parameter(name: str, value: Any, source: ParameterSource, feature_id: str | None = None) -> None:
        if value is None:
            return
        parameter_id = unique_identifier(_slugify(name), seen)
        parameters.append(
            Parameter(
                parameter_id=parameter_id,
                name=name,
                value=float(value),
                source=source,
                feature_id=feature_id,
            )
        )

    for name, value in (plan_data.get("dimensions") or {}).items():
        add_parameter(str(name), value, ParameterSource.DIMENSION)

    design_brief = _mapping_view(plan_data.get("design_brief"))
    for item in design_brief.get("critical_dimensions") or ():
        dimension = _mapping_view(item)
        add_parameter(
            str(dimension.get("name") or "critical-dimension"),
            dimension.get("value"),
            ParameterSource.CRITICAL_DIMENSION,
        )

    modification_plan = _mapping_view(plan_data.get("modification_plan"))
    for name, value in (modification_plan.get("target_params") or {}).items():
        add_parameter(str(name), value, ParameterSource.DERIVED)

    return tuple(parameters)


def _build_sketches(plan_data: Mapping[str, Any], feature_id: str) -> tuple[Sketch, ...]:
    part_type = str(
        plan_data.get("part_type")
        or plan_data.get("model_kind")
        or plan_data.get("artifact_type")
        or "custom"
    )
    if part_type == "assembly":
        return ()
    plane = "xy"
    if part_type in {"profile_2d", "revolution"}:
        plane = "xy"
    elif part_type in {"swept"}:
        plane = "custom"
    return (
        Sketch(
            sketch_id=bounded_identifier(f"{feature_id}-sketch"),
            plane=plane,
            feature_id=feature_id,
            entities=(
                SketchEntity(
                    entity_id=bounded_identifier(f"{feature_id}-profile"),
                    entity_type=SketchEntityType.PROFILE,
                    parameters={"source": part_type},
                ),
            ),
            notes="Derived sketch anchor with no explicit entity-level geometry yet.",
        ),
    )


def _build_features(plan_data: Mapping[str, Any]) -> tuple[Feature, ...]:
    features: list[Feature] = []
    for feature_id, name in feature_candidates(plan_data):
        feature_type = _feature_type_from_text(name)
        features.append(
            Feature(
                feature_id=feature_id,
                feature_type=feature_type,
                parameters={"description": name},
                notes="Derived from the planning brief; shape details remain unresolved at this stage.",
            )
        )
    return tuple(features)


def build_geometry_plan(plan_like: Any) -> GeometryPlan:
    plan_data = _mapping_view(plan_like)
    design_brief = _mapping_view(plan_data.get("design_brief"))
    objective = str(
        plan_data.get("objective")
        or plan_data.get("description")
        or design_brief.get("intent_summary")
        or _primary_feature_name(plan_data)
    ).strip() or "CAD objective"
    artifact_type = str(
        design_brief.get("artifact_type")
        or plan_data.get("artifact_type")
        or plan_data.get("part_type")
        or plan_data.get("model_kind")
        or "custom"
    )
    features = _build_features(plan_data)
    primary_feature_id = features[0].feature_id if features else "main-body"
    parameters = _build_parameters(plan_data)
    verification_targets = build_verification_targets(plan_like)
    target_ids = tuple(target.target_id for target in verification_targets)
    references = tuple(
        FeatureRef(
            source_id=feature.feature_id,
            target_id=target.target_id,
            relation="drives_verification",
        )
        for feature in features
        for target in verification_targets
        if target.feature_id == feature.feature_id
        or (target.feature_id is None and feature is features[0])
    )
    sketches = _build_sketches(plan_data, primary_feature_id)

    notes = [
        "Derived CAD IR is intentionally conservative.",
        "Unresolved geometry keeps placeholder entities instead of inventing topology.",
    ]
    if not verification_targets:
        notes.append("No explicit verification targets could be derived from the plan.")

    return GeometryPlan(
        objective=objective,
        artifact_type=artifact_type,
        parameters=parameters,
        sketches=sketches,
        features=features,
        verification_target_ids=target_ids,
        references=references,
        notes=tuple(notes),
    )


def render_geometry_plan(plan_like: Any) -> str:
    geometry_plan = build_geometry_plan(plan_like)
    verification_targets = build_verification_targets(plan_like)
    lines = [
        f"Objective: {geometry_plan.objective}",
        f"Artifact type: {geometry_plan.artifact_type}",
        "Parameters:",
    ]
    if geometry_plan.parameters:
        lines.extend(
            f"- {item.name}={item.value:g} {item.unit} ({item.source.value})"
            for item in geometry_plan.parameters
        )
    else:
        lines.append("- no explicit scalar parameters")
    lines.append("Features:")
    if geometry_plan.features:
        lines.extend(
            f"- {item.feature_id}: {item.feature_type.value}"
            f" ({item.parameters.get('description', 'no description')})"
            for item in geometry_plan.features
        )
    else:
        lines.append("- no explicit features")
    lines.append("Verification targets:")
    lines.append(summarize_verification_targets(verification_targets))
    if geometry_plan.notes:
        lines.append("Notes:")
        lines.extend(f"- {note}" for note in geometry_plan.notes)
    return "\n".join(lines)


def summarize_geometry_plan(plan_like: Any) -> str:
    geometry_plan = build_geometry_plan(plan_like)
    feature_names = ", ".join(
        f"{item.feature_id}:{item.feature_type.value}"
        for item in geometry_plan.features[:5]
    ) or "no explicit features"
    parameter_names = ", ".join(item.name for item in geometry_plan.parameters[:5]) or "no explicit parameters"
    return (
        f"geometry-plan.v1 | artifact={geometry_plan.artifact_type} | "
        f"features={feature_names} | parameters={parameter_names} | "
        f"verification_targets={len(geometry_plan.verification_target_ids)}"
    )
