from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.geometry_ir.contracts import Axis


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VerificationType(str, Enum):
    OVERALL_DIMENSION = "overall_dimension"
    HOLE_DIAMETER = "hole_diameter"
    HOLE_POSITION = "hole_position"
    HOLE_DISTANCE = "hole_distance"
    HOLE_DEPTH = "hole_depth"
    WALL_THICKNESS = "wall_thickness"
    EDGE_MARGIN = "edge_margin"
    FILLET_RADIUS = "fillet_radius"
    VOLUME = "volume"
    SURFACE_AREA = "surface_area"
    MASS = "mass"
    PARALLELISM = "parallelism"
    PERPENDICULARITY = "perpendicularity"
    CONCENTRICITY = "concentricity"
    FLATNESS = "flatness"
    COUNTERBORE_DIAMETER = "counterbore_diameter"
    COUNTERBORE_DEPTH = "counterbore_depth"
    WATER_TIGHTNESS = "water_tightness"
    SINGLE_BODY = "single_body"
    CUSTOM = "custom"


class VerificationTarget(FrozenContract):
    target_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    target_type: VerificationType
    nominal: float | None = None
    tolerance_upper: float | None = Field(default=None, ge=0)
    tolerance_lower: float | None = Field(default=None, ge=0)
    feature_id: str | None = Field(default=None, min_length=1, max_length=120)
    reference_feature_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
    )
    measurement_axis: Axis | None = None
    severity: Literal["required", "advisory"] = "required"
    description: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def tolerances_are_valid(self) -> "VerificationTarget":
        if (
            self.tolerance_upper is None
            and self.tolerance_lower is None
            and self.nominal is not None
        ):
            # A target may be explicit enough to carry only a nominal value.
            return self
        return self


class VerificationEvidence(FrozenContract):
    target_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    outcome: Literal["passed", "failed", "indeterminate"]
    measured_value: float | None = None
    expected_value: float | None = None
    deviation: float | None = None
    evidence_ref: str | None = Field(default=None, max_length=255)
    details: dict[str, object] = Field(default_factory=dict)

