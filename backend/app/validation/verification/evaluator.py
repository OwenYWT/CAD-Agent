from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.geometry_ir.contracts import Axis
from app.validation.durable_geometry import DurableGeometryReport

from .contracts import VerificationEvidence, VerificationTarget, VerificationType


_DIMENSION_TOKEN_MAP: tuple[tuple[str, VerificationType], ...] = (
    ("counterbore_depth", VerificationType.COUNTERBORE_DEPTH),
    ("counterbore_dia", VerificationType.COUNTERBORE_DIAMETER),
    ("counterbore", VerificationType.COUNTERBORE_DIAMETER),
    ("hole_depth", VerificationType.HOLE_DEPTH),
    ("hole_distance", VerificationType.HOLE_DISTANCE),
    ("hole_spacing", VerificationType.HOLE_DISTANCE),
    ("hole_pos", VerificationType.HOLE_POSITION),
    ("hole_position", VerificationType.HOLE_POSITION),
    ("hole_dia", VerificationType.HOLE_DIAMETER),
    ("hole_radius", VerificationType.HOLE_DIAMETER),
    ("wall_thickness", VerificationType.WALL_THICKNESS),
    ("wall", VerificationType.WALL_THICKNESS),
    ("edge_margin", VerificationType.EDGE_MARGIN),
    ("fillet_radius", VerificationType.FILLET_RADIUS),
    ("fillet", VerificationType.FILLET_RADIUS),
    ("volume", VerificationType.VOLUME),
    ("surface_area", VerificationType.SURFACE_AREA),
    ("mass", VerificationType.MASS),
    ("overall", VerificationType.OVERALL_DIMENSION),
)


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
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return cleaned or "target"


def _infer_axis(text: str) -> Axis | None:
    lowered = text.casefold()
    if re.search(r"\bx\b|horizontal|width", lowered):
        return Axis.X
    if re.search(r"\by\b|depth|length", lowered):
        return Axis.Y
    if re.search(r"\bz\b|vertical|height|thickness", lowered):
        return Axis.Z
    return None


def _infer_target_type(text: str) -> VerificationType:
    lowered = text.casefold().replace(" ", "_")
    for needle, target_type in _DIMENSION_TOKEN_MAP:
        if needle in lowered:
            return target_type
    return VerificationType.OVERALL_DIMENSION


def _feature_candidates(plan_data: Mapping[str, Any]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    for item in plan_data.get("features") or []:
        text = str(item).strip()
        if text:
            candidates.append((_slugify(text), text))
    for step in plan_data.get("steps") or []:
        step_data = _mapping_view(step)
        text = str(step_data.get("description") or step_data.get("step_key") or "").strip()
        if text:
            candidates.append((_slugify(str(step_data.get("step_key") or text)), text))
    return candidates


def _match_feature_id(text: str, candidates: Sequence[tuple[str, str]]) -> str | None:
    lowered = text.casefold()
    for candidate_id, candidate_text in candidates:
        candidate_tokens = [item for item in re.split(r"[^a-z0-9]+", candidate_text.casefold()) if item]
        if candidate_tokens and all(token in lowered for token in candidate_tokens[:2]):
            return candidate_id
    return candidates[0][0] if candidates else None


def _dimension_sources(plan_data: Mapping[str, Any]) -> list[tuple[str, float | None, str | None, str]]:
    sources: list[tuple[str, float | None, str | None, str]] = []
    design_brief = _mapping_view(plan_data.get("design_brief"))
    for item in design_brief.get("critical_dimensions") or []:
        dimension = _mapping_view(item)
        name = str(dimension.get("name") or "dimension")
        value = dimension.get("value")
        sources.append((name, float(value) if value is not None else None, None, "required"))
    for name, value in (plan_data.get("dimensions") or {}).items():
        sources.append((str(name), float(value) if value is not None else None, None, "required"))
    modification_plan = _mapping_view(plan_data.get("modification_plan"))
    for name, value in (modification_plan.get("target_params") or {}).items():
        sources.append((str(name), float(value) if value is not None else None, None, "required"))
    return sources


def _advisory_sources(plan_data: Mapping[str, Any]) -> list[tuple[str, VerificationType]]:
    design_brief = _mapping_view(plan_data.get("design_brief"))
    texts = [
        *(str(item) for item in design_brief.get("acceptance_criteria") or ()),
        *(str(item) for item in design_brief.get("printability_targets") or ()),
        *(str(item) for item in design_brief.get("functional_requirements") or ()),
    ]
    targets: list[tuple[str, VerificationType]] = []
    for text in texts:
        lowered = text.casefold()
        if any(marker in lowered for marker in ("封闭", "watertight", "无破面", "water tight")):
            targets.append((text, VerificationType.WATER_TIGHTNESS))
        if any(marker in lowered for marker in ("单体", "single body", "单一实体")):
            targets.append((text, VerificationType.SINGLE_BODY))
        if any(marker in lowered for marker in ("体积", "volume")):
            targets.append((text, VerificationType.VOLUME))
    return targets


def build_verification_targets(plan_like: Any) -> tuple[VerificationTarget, ...]:
    plan_data = _mapping_view(plan_like)
    candidates = _feature_candidates(plan_data)
    targets: list[VerificationTarget] = []
    seen_ids: set[str] = set()

    for name, value, _, severity in _dimension_sources(plan_data):
        target_type = _infer_target_type(name)
        target_id = _slugify(name)
        suffix = 2
        while target_id in seen_ids:
            target_id = f"{_slugify(name)}-{suffix:02d}"
            suffix += 1
        seen_ids.add(target_id)
        targets.append(
            VerificationTarget(
                target_id=target_id,
                target_type=target_type,
                nominal=value,
                tolerance_upper=None,
                tolerance_lower=None,
                feature_id=_match_feature_id(name, candidates),
                measurement_axis=_infer_axis(name),
                severity=severity,
                description=name,
            )
        )

    for text, target_type in _advisory_sources(plan_data):
        target_id = _slugify(text)
        suffix = 2
        while target_id in seen_ids:
            target_id = f"{_slugify(text)}-{suffix:02d}"
            suffix += 1
        seen_ids.add(target_id)
        targets.append(
            VerificationTarget(
                target_id=target_id,
                target_type=target_type,
                severity="advisory",
                description=text,
                feature_id=_match_feature_id(text, candidates),
            )
        )

    return tuple(targets)


def evaluate_verification_targets(
    targets: Sequence[VerificationTarget],
    report: DurableGeometryReport,
    *,
    evidence_ref: str | None = None,
) -> tuple[VerificationEvidence, ...]:
    artifacts = tuple(report.artifacts)
    first = artifacts[0] if artifacts else None
    max_dimension_error = first.max_dimension_error if first else None
    evaluation: list[VerificationEvidence] = []

    for target in targets:
        details: dict[str, object] = {
            "target_type": target.target_type.value,
            "severity": target.severity,
            "artifact_kind": report.artifact_kind,
        }
        if target.feature_id is not None:
            details["feature_id"] = target.feature_id

        if target.target_type is VerificationType.SINGLE_BODY:
            solid_counts = [item.solid_count for item in artifacts]
            if solid_counts and all(count is not None for count in solid_counts):
                outcome = "passed" if all(count == 1 for count in solid_counts if count is not None) else "failed"
                deviation = float(sum(max(0, (count or 0) - 1) for count in solid_counts))
                measured_value = float(solid_counts[0]) if solid_counts[0] is not None else None
            else:
                outcome = "indeterminate"
                deviation = None
                measured_value = None
            details["solid_counts"] = solid_counts
            evaluation.append(
                VerificationEvidence(
                    target_id=target.target_id,
                    outcome=outcome,
                    measured_value=measured_value,
                    expected_value=1.0,
                    deviation=deviation,
                    evidence_ref=evidence_ref,
                    details=details,
                )
            )
            continue

        if target.target_type is VerificationType.WATER_TIGHTNESS:
            watertight_values = [item.is_watertight for item in artifacts]
            if watertight_values and all(value is not None for value in watertight_values):
                outcome = "passed" if all(bool(value) for value in watertight_values) else "failed"
                measured_value = 1.0 if all(bool(value) for value in watertight_values) else 0.0
                deviation = 0.0 if outcome == "passed" else 1.0
            else:
                outcome = "indeterminate"
                measured_value = None
                deviation = None
            details["is_watertight"] = watertight_values
            evaluation.append(
                VerificationEvidence(
                    target_id=target.target_id,
                    outcome=outcome,
                    measured_value=measured_value,
                    expected_value=1.0,
                    deviation=deviation,
                    evidence_ref=evidence_ref,
                    details=details,
                )
            )
            continue

        if target.target_type is VerificationType.VOLUME:
            volumes = [item.volume_mm3 for item in artifacts if item.volume_mm3 is not None]
            if target.nominal is not None and len(volumes) == 1:
                measured_value = float(volumes[0])
                deviation = abs(measured_value - target.nominal)
                tolerance = (
                    target.tolerance_upper
                    if target.tolerance_upper is not None
                    else target.tolerance_lower
                    if target.tolerance_lower is not None
                    else report.dimension_tolerance
                )
                outcome = "passed" if deviation <= tolerance else "failed"
                expected_value = target.nominal
            else:
                measured_value = None
                expected_value = target.nominal
                deviation = None
                outcome = "indeterminate"
            details["volumes_mm3"] = volumes
            evaluation.append(
                VerificationEvidence(
                    target_id=target.target_id,
                    outcome=outcome,
                    measured_value=measured_value,
                    expected_value=expected_value,
                    deviation=deviation,
                    evidence_ref=evidence_ref,
                    details=details,
                )
            )
            continue

        if target.target_type is VerificationType.OVERALL_DIMENSION:
            if max_dimension_error is not None:
                tolerance = (
                    target.tolerance_upper
                    if target.tolerance_upper is not None
                    else target.tolerance_lower
                    if target.tolerance_lower is not None
                    else report.dimension_tolerance
                )
                outcome = "passed" if max_dimension_error <= tolerance else "failed"
                deviation = float(max_dimension_error)
            else:
                outcome = "indeterminate"
                deviation = None
            details["max_dimension_error"] = max_dimension_error
            evaluation.append(
                VerificationEvidence(
                    target_id=target.target_id,
                    outcome=outcome,
                    measured_value=None,
                    expected_value=target.nominal,
                    deviation=deviation,
                    evidence_ref=evidence_ref,
                    details=details,
                )
            )
            continue

        details["report_issues"] = list(report.issues)
        evaluation.append(
            VerificationEvidence(
                target_id=target.target_id,
                outcome="indeterminate",
                measured_value=None,
                expected_value=target.nominal,
                deviation=None,
                evidence_ref=evidence_ref,
                details=details,
            )
        )

    return tuple(evaluation)


def summarize_verification_targets(targets: Sequence[VerificationTarget]) -> str:
    lines: list[str] = []
    for target in targets:
        tolerance = []
        if target.tolerance_lower is not None:
            tolerance.append(f"-{target.tolerance_lower:g}")
        if target.tolerance_upper is not None:
            tolerance.append(f"+{target.tolerance_upper:g}")
        tolerance_text = f" ({'/'.join(tolerance)})" if tolerance else ""
        nominal_text = f"={target.nominal:g} mm" if target.nominal is not None else ""
        feature_text = f", feature={target.feature_id}" if target.feature_id else ""
        axis_text = f", axis={target.measurement_axis.value}" if target.measurement_axis else ""
        lines.append(
            f"- {target.target_id}: {target.target_type.value}{nominal_text}{tolerance_text}"
            f", severity={target.severity}{feature_text}{axis_text}"
        )
    return "\n".join(lines) if lines else "- no explicit verification targets"


def summarize_verification_evidence(
    evidence: Sequence[VerificationEvidence],
) -> str:
    lines: list[str] = []
    for item in evidence:
        measured = (
            f"measured={item.measured_value:g}"
            if item.measured_value is not None
            else "measured=unavailable"
        )
        expected = (
            f"expected={item.expected_value:g}"
            if item.expected_value is not None
            else "expected=unavailable"
        )
        deviation = (
            f"deviation={item.deviation:g}"
            if item.deviation is not None
            else "deviation=unavailable"
        )
        lines.append(
            f"- {item.target_id}: {item.outcome} ({measured}, {expected}, {deviation})"
        )
    return "\n".join(lines) if lines else "- no verification evidence"
