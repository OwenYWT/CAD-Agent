from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Axis(str, Enum):
    X = "x"
    Y = "y"
    Z = "z"
    CUSTOM = "custom"


class ParameterSource(str, Enum):
    DIMENSION = "dimension"
    CRITICAL_DIMENSION = "critical_dimension"
    FEATURE = "feature"
    DERIVED = "derived"


class SketchEntityType(str, Enum):
    POINT = "point"
    LINE = "line"
    ARC = "arc"
    CIRCLE = "circle"
    RECTANGLE = "rectangle"
    SLOT = "slot"
    POLYLINE = "polyline"
    SPLINE = "spline"
    PROFILE = "profile"
    CUSTOM = "custom"


class FeatureType(str, Enum):
    EXTRUDE = "extrude"
    REVOLVE = "revolve"
    SWEEP = "sweep"
    LOFT = "loft"
    HOLE = "hole"
    FILLET = "fillet"
    CHAMFER = "chamfer"
    SHELL = "shell"
    PATTERN = "pattern"
    BOOLEAN = "boolean"
    MIRROR = "mirror"
    RIB = "rib"
    DRAFT = "draft"
    WORKPLANE = "workplane"
    SKETCH = "sketch"
    CUSTOM = "custom"


class Parameter(FrozenContract):
    parameter_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    name: str = Field(min_length=1, max_length=120)
    value: float
    unit: str = Field(default="mm", min_length=1, max_length=32)
    source: ParameterSource = ParameterSource.DERIVED
    feature_id: str | None = Field(default=None, min_length=1, max_length=120)
    notes: str = Field(default="", max_length=4000)


class SketchEntity(FrozenContract):
    entity_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    entity_type: SketchEntityType = SketchEntityType.CUSTOM
    parameters: dict[str, Any] = Field(default_factory=dict)
    references: tuple[str, ...] = ()


class Sketch(FrozenContract):
    sketch_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    plane: Literal["xy", "xz", "yz", "custom"] = "xy"
    feature_id: str | None = Field(default=None, min_length=1, max_length=120)
    entities: tuple[SketchEntity, ...] = ()
    constraints: tuple[str, ...] = ()
    notes: str = Field(default="", max_length=4000)


class FeatureRef(FrozenContract):
    source_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    target_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    relation: Literal[
        "depends_on",
        "drives_verification",
        "binds_topology",
        "uses_sketch",
    ] = "drives_verification"


class Feature(FrozenContract):
    feature_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    feature_type: FeatureType = FeatureType.CUSTOM
    depends_on: tuple[str, ...] = ()
    parameters: dict[str, Any] = Field(default_factory=dict)
    target_refs: tuple[str, ...] = ()
    sketch_refs: tuple[str, ...] = ()
    topology_binding_ids: tuple[str, ...] = ()
    notes: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def references_are_unique(self) -> "Feature":
        for label, values in (
            ("depends_on", self.depends_on),
            ("target_refs", self.target_refs),
            ("sketch_refs", self.sketch_refs),
            ("topology_binding_ids", self.topology_binding_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} cannot contain duplicates")
        return self


class GeometryPlan(FrozenContract):
    schema_version: Literal["geometry-plan.v1"] = "geometry-plan.v1"
    objective: str = Field(min_length=1, max_length=4000)
    artifact_type: str = Field(min_length=1, max_length=120)
    parameters: tuple[Parameter, ...] = ()
    sketches: tuple[Sketch, ...] = ()
    features: tuple[Feature, ...] = ()
    verification_target_ids: tuple[str, ...] = ()
    references: tuple[FeatureRef, ...] = ()
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def references_are_unique(self) -> "GeometryPlan":
        if len(self.parameters) != len({item.parameter_id for item in self.parameters}):
            raise ValueError("geometry parameters must have unique identifiers")
        if len(self.sketches) != len({item.sketch_id for item in self.sketches}):
            raise ValueError("geometry sketches must have unique identifiers")
        if len(self.features) != len({item.feature_id for item in self.features}):
            raise ValueError("geometry features must have unique identifiers")
        if len(self.verification_target_ids) != len(set(self.verification_target_ids)):
            raise ValueError("verification target identifiers must be unique")
        if len(self.references) != len(
            {(item.source_id, item.target_id, item.relation) for item in self.references}
        ):
            raise ValueError("geometry references must be unique")
        return self
