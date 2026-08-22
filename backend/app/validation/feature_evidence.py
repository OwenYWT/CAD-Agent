from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.geometry_ir.contracts import GeometryPlan
from app.topology.contracts import BackendObjectRef, TopologyResolution
from app.validation.durable_geometry import DurableGeometryReport, GeometryBounds
from app.validation.verification.contracts import VerificationEvidence, VerificationTarget


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeatureEvidence(FrozenContract):
    feature_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    feature_type: str = Field(min_length=1, max_length=120)
    step_key: str = Field(min_length=1, max_length=120)
    bounds: GeometryBounds | None = None
    volume_mm3: float | None = Field(default=None, ge=0)
    solid_count: int | None = Field(default=None, ge=0)
    face_count: int | None = Field(default=None, ge=0)
    valid: bool
    issues: tuple[str, ...] = ()
    source_hash: str = Field(min_length=1, max_length=128)
    manifest_id: str | None = Field(default=None, min_length=1, max_length=255)
    geometry_evidence_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
    )
    verification_target_ids: tuple[str, ...] = ()
    topology_resolution_ids: tuple[str, ...] = ()
    backend_refs: tuple[BackendObjectRef, ...] = ()
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def references_are_unique(self) -> "FeatureEvidence":
        if len(self.issues) != len(set(self.issues)):
            raise ValueError("feature evidence issues must be unique")
        if len(self.verification_target_ids) != len(set(self.verification_target_ids)):
            raise ValueError("verification target identifiers must be unique")
        if len(self.topology_resolution_ids) != len(set(self.topology_resolution_ids)):
            raise ValueError("topology resolution identifiers must be unique")
        if len(self.backend_refs) != len(
            {
                (item.backend, item.object_id, item.role, item.revision_id)
                for item in self.backend_refs
            }
        ):
            raise ValueError("backend refs must be unique")
        return self


class FeatureExecutionResult(FrozenContract):
    feature_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    evidence: FeatureEvidence
    backend_refs: tuple[BackendObjectRef, ...] = ()


def _mapping_view(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="python")
    raise TypeError(f"unsupported structured object: {type(value)!r}")


def _artifact_refs(executed: Mapping[str, Any], manifest_id: str | None) -> tuple[BackendObjectRef, ...]:
    refs: list[BackendObjectRef] = []
    if manifest_id:
        refs.append(
            BackendObjectRef(
                backend="step",
                object_id=manifest_id,
                role="manifest",
            )
        )
    for index, output in enumerate(executed.get("outputs") or ()):
        output_data = _mapping_view(output)
        object_id = str(output_data.get("object_key") or output_data.get("filename") or f"output-{index:02d}")
        refs.append(
            BackendObjectRef(
                backend="step",
                object_id=object_id,
                role=str(output_data.get("format") or output_data.get("role") or "output"),
                revision_id=str(output_data.get("hash") or output_data.get("sha256") or "") or None,
            )
        )
    return tuple(refs)


def _first_artifact(report: DurableGeometryReport | None):
    if report is None or not report.artifacts:
        return None
    return report.artifacts[0]


def build_feature_evidence(
    *,
    step: Mapping[str, Any] | Any,
    generated: Mapping[str, Any] | Any,
    executed: Mapping[str, Any] | Any,
    geometry_report: DurableGeometryReport | None = None,
    verification_evidence: Sequence[VerificationEvidence] = (),
    topology_resolutions: Sequence[TopologyResolution] = (),
    geometry_plan: GeometryPlan | None = None,
) -> FeatureExecutionResult:
    step_data = _mapping_view(step)
    generated_data = _mapping_view(generated)
    executed_data = _mapping_view(executed)
    feature_id = str(step_data.get("step_key") or step_data.get("feature_id") or "feature")
    feature_type = str(step_data.get("kind") or generated_data.get("generator_kind") or "custom")
    geometry_artifact = _first_artifact(geometry_report)
    issues: list[str] = []
    notes: list[str] = []

    if geometry_report is not None:
        issues.extend(str(item) for item in geometry_report.issues if item)
        if geometry_artifact is not None:
            issues.extend(str(item) for item in geometry_artifact.issues if item)

    for item in verification_evidence:
        if item.outcome == "failed":
            issues.append(f"verification_failed:{item.target_id}")
        elif item.outcome == "indeterminate":
            notes.append(f"verification_indeterminate:{item.target_id}")

    unresolved_topology = [item for item in topology_resolutions if item.resolution_state == "unresolved"]
    if unresolved_topology:
        notes.append(
            "topology unresolved: "
            + ", ".join(item.target_id for item in unresolved_topology)
        )

    source_hash = str(
        generated_data.get("source_hash")
        or executed_data.get("source_hash")
        or generated_data.get("manifest_hash")
        or executed_data.get("manifest_hash")
        or ""
    )
    if not source_hash:
        raise ValueError("feature evidence requires a source hash")

    manifest_id = str(executed_data.get("staging_manifest_id") or "") or None
    backend_refs = _artifact_refs(executed_data, manifest_id)
    if geometry_plan is not None and geometry_plan.verification_target_ids:
        notes.append(
            f"geometry plan targets: {', '.join(geometry_plan.verification_target_ids)}"
        )

    evidence = FeatureEvidence(
        feature_id=feature_id,
        feature_type=feature_type,
        step_key=str(step_data.get("step_key") or feature_id),
        bounds=(geometry_artifact.bounds_mm if geometry_artifact else None),
        volume_mm3=(geometry_artifact.volume_mm3 if geometry_artifact else None),
        solid_count=(geometry_artifact.solid_count if geometry_artifact else None),
        face_count=(geometry_artifact.face_count if geometry_artifact else None),
        valid=bool(geometry_report and geometry_report.outcome == "passed"),
        issues=tuple(dict.fromkeys(issues)),
        source_hash=source_hash,
        manifest_id=manifest_id,
        geometry_evidence_id=str(executed_data.get("geometry_evidence_id") or "") or None,
        verification_target_ids=tuple(
            dict.fromkeys(item.target_id for item in verification_evidence)
        ),
        topology_resolution_ids=tuple(
            dict.fromkeys(item.target_id for item in topology_resolutions)
        ),
        backend_refs=backend_refs,
        notes=tuple(dict.fromkeys(notes)),
    )
    return FeatureExecutionResult(
        feature_id=feature_id,
        evidence=evidence,
        backend_refs=backend_refs,
    )


def build_feature_repair_context(
    *,
    geometry_plan: GeometryPlan,
    verification_targets: Sequence[VerificationTarget],
    verification_evidence: Sequence[VerificationEvidence],
    topology_resolutions: Sequence[TopologyResolution],
    feature_evidence: FeatureEvidence,
    geometry_report: DurableGeometryReport,
    expected_dimensions_mm: dict[str, float],
) -> dict[str, Any]:
    return {
        "geometry_plan": geometry_plan.model_dump(mode="json"),
        "verification_targets": [
            item.model_dump(mode="json") for item in verification_targets
        ],
        "verification_evidence": [
            item.model_dump(mode="json") for item in verification_evidence
        ],
        "topology_resolutions": [
            item.model_dump(mode="json") for item in topology_resolutions
        ],
        "feature_evidence": feature_evidence.model_dump(mode="json"),
        "geometry_report": geometry_report.model_dump(mode="json"),
        "expected_dimensions_mm": dict(expected_dimensions_mm),
        "suspected_feature_ids": [feature_evidence.feature_id],
    }


def summarize_feature_evidence(evidence: FeatureEvidence) -> str:
    bounds = (
        "["
        f"x=({evidence.bounds.x_min:g}, {evidence.bounds.x_max:g}), "
        f"y=({evidence.bounds.y_min:g}, {evidence.bounds.y_max:g}), "
        f"z=({evidence.bounds.z_min:g}, {evidence.bounds.z_max:g})"
        "]"
        if evidence.bounds is not None
        else "unavailable"
    )
    return (
        f"feature={evidence.feature_id} type={evidence.feature_type} step={evidence.step_key} "
        f"valid={evidence.valid} volume={evidence.volume_mm3 if evidence.volume_mm3 is not None else 'n/a'} "
        f"solid_count={evidence.solid_count if evidence.solid_count is not None else 'n/a'} "
        f"bounds={bounds} issues={len(evidence.issues)}"
    )
