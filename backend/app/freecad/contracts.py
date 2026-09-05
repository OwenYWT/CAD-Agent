"""Versioned contracts for the allowlisted FreeCAD capability."""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.topology.contracts import FreeCADTopologySelector


ObjectName = Annotated[
    str,
    Field(min_length=1, max_length=80, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
]
OperationId = Annotated[
    str,
    Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$"),
]
Millimetres = Annotated[float, Field(gt=0, le=1_000_000)]
Coordinate = Annotated[float, Field(ge=-1_000_000, le=1_000_000)]


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Point2D(FrozenContract):
    x: Coordinate
    y: Coordinate


class LineGeometry(FrozenContract):
    kind: Literal["line"] = "line"
    start: Point2D
    end: Point2D
    construction: bool = False

    @model_validator(mode="after")
    def non_zero_length(self) -> "LineGeometry":
        if self.start == self.end:
            raise ValueError("line geometry must have non-zero length")
        return self


class CircleGeometry(FrozenContract):
    kind: Literal["circle"] = "circle"
    center: Point2D
    radius_mm: Millimetres
    construction: bool = False


class RectangleGeometry(FrozenContract):
    kind: Literal["rectangle"] = "rectangle"
    corner: Point2D
    width_mm: Millimetres
    height_mm: Millimetres
    construction: bool = False


SketchGeometry = Annotated[
    LineGeometry | CircleGeometry | RectangleGeometry,
    Field(discriminator="kind"),
]


class ConstraintReference(FrozenContract):
    geometry_index: int = Field(ge=0, le=100_000)
    point_position: Literal[1, 2, 3] | None = None


class DocumentInspectArgs(FrozenContract):
    pass


class SketchCreateArgs(FrozenContract):
    name: ObjectName
    body: ObjectName = "Body"
    plane: Literal["xy", "xz", "yz"] = "xy"
    offset_mm: Coordinate = 0.0
    reversed: bool = False


class SketchAddGeometryArgs(FrozenContract):
    sketch: ObjectName
    geometry: SketchGeometry


class SketchAddConstraintArgs(FrozenContract):
    sketch: ObjectName
    kind: Literal[
        "horizontal",
        "vertical",
        "distance_x",
        "distance_y",
        "distance",
        "radius",
        "diameter",
        "coincident",
        "equal",
    ]
    first: ConstraintReference
    second: ConstraintReference | None = None
    value_mm: float | None = Field(default=None, gt=0, le=1_000_000)

    @model_validator(mode="after")
    def valid_signature(self) -> "SketchAddConstraintArgs":
        requires_value = self.kind in {
            "distance_x",
            "distance_y",
            "distance",
            "radius",
            "diameter",
        }
        if requires_value != (self.value_mm is not None):
            raise ValueError(f"{self.kind} value_mm requirement is not satisfied")
        requires_second = self.kind in {"coincident", "equal"}
        if requires_second != (self.second is not None):
            raise ValueError(f"{self.kind} second reference requirement is not satisfied")
        if self.kind in {"distance_x", "distance_y", "coincident"}:
            references = (self.first,) if self.second is None else (self.first, self.second)
            if any(reference.point_position is None for reference in references):
                raise ValueError(f"{self.kind} requires point_position")
        return self


class FeaturePadArgs(FrozenContract):
    name: ObjectName
    profile: ObjectName
    length_mm: Millimetres
    reversed: bool = False


class FeaturePocketArgs(FrozenContract):
    name: ObjectName
    profile: ObjectName
    length_mm: Millimetres | None = None
    through_all: bool = False
    reversed: bool = False

    @model_validator(mode="after")
    def length_or_through_all(self) -> "FeaturePocketArgs":
        if self.through_all == (self.length_mm is not None):
            raise ValueError("pocket requires exactly one of length_mm or through_all")
        return self


class FeatureHoleArgs(FrozenContract):
    name: ObjectName
    profile: ObjectName
    diameter_mm: Millimetres
    depth_mm: Millimetres | None = None
    through_all: bool = False
    reversed: bool = False

    @model_validator(mode="after")
    def depth_or_through_all(self) -> "FeatureHoleArgs":
        if self.through_all == (self.depth_mm is not None):
            raise ValueError("hole requires exactly one of depth_mm or through_all")
        return self


class FeatureFilletArgs(FrozenContract):
    name: ObjectName
    target: ObjectName
    radius_mm: Millimetres
    use_all_edges: bool = True
    selector: FreeCADTopologySelector | None = None

    @model_validator(mode="after")
    def selection_mode(self) -> "FeatureFilletArgs":
        if self.use_all_edges == (self.selector is not None):
            raise ValueError("fillet requires exactly one of use_all_edges or selector")
        if self.selector is not None and self.selector.subelement_kind != "edge":
            raise ValueError("fillet selector must resolve an edge")
        return self


class FeatureChamferArgs(FrozenContract):
    name: ObjectName
    target: ObjectName
    size_mm: Millimetres
    use_all_edges: bool = True
    selector: FreeCADTopologySelector | None = None

    @model_validator(mode="after")
    def selection_mode(self) -> "FeatureChamferArgs":
        if self.use_all_edges == (self.selector is not None):
            raise ValueError("chamfer requires exactly one of use_all_edges or selector")
        if self.selector is not None and self.selector.subelement_kind != "edge":
            raise ValueError("chamfer selector must resolve an edge")
        return self


PropertyValue = str | int | float | bool


class PropertySetArgs(FrozenContract):
    object: ObjectName
    property: ObjectName
    value: PropertyValue
    expected_property_type: str | None = Field(default=None, max_length=100)
    unit: Literal["mm", "deg"] | None = None

    @model_validator(mode="after")
    def finite_number(self) -> "PropertySetArgs":
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("property value must be finite")
        return self


class DocumentExportArgs(FrozenContract):
    formats: tuple[Literal["fcstd", "step", "stl", "dxf"], ...] = (
        "fcstd",
        "step",
    )
    basename: ObjectName = "model"

    @model_validator(mode="after")
    def unique_formats(self) -> "DocumentExportArgs":
        if not self.formats or len(self.formats) != len(set(self.formats)):
            raise ValueError("document export formats must be non-empty and unique")
        if "fcstd" not in self.formats:
            raise ValueError("FreeCAD executions must retain an fcstd artifact")
        return self


OperationArgs = (
    DocumentInspectArgs
    | SketchCreateArgs
    | SketchAddGeometryArgs
    | SketchAddConstraintArgs
    | FeaturePadArgs
    | FeaturePocketArgs
    | FeatureHoleArgs
    | FeatureFilletArgs
    | FeatureChamferArgs
    | PropertySetArgs
    | DocumentExportArgs
)


_ACTION_ARG_TYPES: dict[str, type[FrozenContract]] = {
    "document.inspect": DocumentInspectArgs,
    "sketch.create": SketchCreateArgs,
    "sketch.add_geometry": SketchAddGeometryArgs,
    "sketch.add_constraint": SketchAddConstraintArgs,
    "feature.pad": FeaturePadArgs,
    "feature.pocket": FeaturePocketArgs,
    "feature.hole": FeatureHoleArgs,
    "feature.fillet": FeatureFilletArgs,
    "feature.chamfer": FeatureChamferArgs,
    "property.set": PropertySetArgs,
    "document.export": DocumentExportArgs,
}


class FreeCADOperation(FrozenContract):
    op_id: OperationId
    action: Literal[
        "document.inspect",
        "sketch.create",
        "sketch.add_geometry",
        "sketch.add_constraint",
        "feature.pad",
        "feature.pocket",
        "feature.hole",
        "feature.fillet",
        "feature.chamfer",
        "property.set",
        "document.export",
    ]
    args: dict

    @model_validator(mode="after")
    def validate_args(self) -> "FreeCADOperation":
        validated = _ACTION_ARG_TYPES[self.action].model_validate(self.args)
        object.__setattr__(self, "args", validated.model_dump(mode="json"))
        return self

    def typed_args(self) -> OperationArgs:
        return _ACTION_ARG_TYPES[self.action].model_validate(self.args)


class FreeCADOperationPlan(FrozenContract):
    schema_version: Literal["freecad-operation-plan.v1"] = (
        "freecad-operation-plan.v1"
    )
    document_name: ObjectName = "Model"
    operations: tuple[FreeCADOperation, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_sequence(self) -> "FreeCADOperationPlan":
        op_ids = [operation.op_id for operation in self.operations]
        if len(op_ids) != len(set(op_ids)):
            raise ValueError("FreeCAD operation IDs must be unique")
        exports = [
            index
            for index, operation in enumerate(self.operations)
            if operation.action == "document.export"
        ]
        if len(exports) != 1 or exports[0] != len(self.operations) - 1:
            raise ValueError("FreeCAD plan requires one final document.export operation")
        return self


class FreeCADOperationError(FrozenContract):
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=4000)
    op_id: OperationId | None = None
    action: str | None = Field(default=None, max_length=120)
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class FreeCADOperationResult(FrozenContract):
    schema_version: Literal["freecad-operation-result.v1"] = (
        "freecad-operation-result.v1"
    )
    status: Literal["succeeded", "failed"]
    operations: tuple[dict[str, str | int | float | bool | None], ...] = ()
    files: dict[str, str] = Field(default_factory=dict)
    validations: tuple[dict[str, str | int | float | bool | None], ...] = ()
    error: FreeCADOperationError | None = None

    @model_validator(mode="after")
    def status_matches_error(self) -> "FreeCADOperationResult":
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("failed result requires error and success forbids error")
        return self
