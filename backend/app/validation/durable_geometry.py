"""Strict wire contract for geometry evidence returned by isolated MCAD workers."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GeometryBounds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float


class GeometryArtifactEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=255)
    format: Literal["step", "stl", "dxf"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    parseable: bool
    valid: bool
    solid_count: int | None = Field(default=None, ge=0)
    face_count: int | None = Field(default=None, ge=0)
    is_watertight: bool | None = None
    volume_mm3: float | None = Field(default=None, ge=0)
    bounds_mm: GeometryBounds | None = None
    dimensions_mm: tuple[float, ...] = ()
    max_dimension_error: float | None = Field(default=None, ge=0)
    issues: tuple[str, ...] = ()

    @model_validator(mode="after")
    def successful_artifact_has_measured_geometry(self):
        if self.valid and (not self.parseable or self.bounds_mm is None):
            raise ValueError("valid geometry requires parseable measured bounds")
        return self


class DurableGeometryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["durable-geometry-report.v1"]
    outcome: Literal["passed", "failed", "indeterminate"]
    artifact_kind: Literal["solid", "profile"]
    expected_dimensions_mm: dict[str, float] = Field(default_factory=dict)
    dimension_tolerance: float = Field(ge=0, le=1)
    artifacts: tuple[GeometryArtifactEvidence, ...]
    issues: tuple[str, ...] = ()

    @model_validator(mode="after")
    def outcome_matches_artifact_evidence(self):
        if not self.artifacts:
            raise ValueError("geometry report requires artifact evidence")
        if self.outcome == "passed" and (
            self.issues or any(not item.valid for item in self.artifacts)
        ):
            raise ValueError("passed geometry cannot contain failed evidence")
        if self.outcome == "indeterminate" and all(
            item.parseable for item in self.artifacts
        ):
            raise ValueError("indeterminate geometry requires an unparseable artifact")
        return self

    def durable_evidence(self, *, runtime_provenance: dict | None) -> dict:
        return {
            **self.model_dump(mode="json"),
            "runtime_provenance": runtime_provenance,
        }


def geometry_failure(report: DurableGeometryReport) -> dict[str, str | None]:
    """Map a failed gate into the existing bounded source-repair taxonomy."""
    return {
        "category": "validation",
        "error_code": "geometry_validation_failed",
        "error_message": "; ".join(report.issues)
        or "Geometry validation did not pass.",
        "runtime_error_type": "GeometryError",
    }


def indeterminate_geometry_report(
    *,
    outputs: tuple[dict, ...],
    expected_dimensions: dict[str, float],
    dimension_tolerance: float,
    issue: str,
) -> DurableGeometryReport:
    return DurableGeometryReport(
        schema_version="durable-geometry-report.v1",
        outcome="indeterminate",
        artifact_kind=(
            "profile"
            if outputs and all(item.get("format") == "dxf" for item in outputs)
            else "solid"
        ),
        expected_dimensions_mm=expected_dimensions,
        dimension_tolerance=dimension_tolerance,
        artifacts=tuple(
            GeometryArtifactEvidence(
                role=f"artifact-{index:02d}",
                filename=str(item["filename"]),
                format=str(item["format"]),
                sha256=str(item["sha256"]),
                size_bytes=int(item["size_bytes"]),
                parseable=False,
                valid=False,
                issues=(issue,),
            )
            for index, item in enumerate(outputs)
        ),
        issues=(issue,),
    )
