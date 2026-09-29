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


class Point3D(Point2D):
    z: Coordinate


class SketchFrame(FrozenContract):
    """An orthogonal sketch frame expressed in its owning Body's coordinates."""
    schema_version: Literal["body-frame.v1"] = "body-frame.v1"
    origin: Point3D
    normal: tuple[Coordinate, Coordinate, Coordinate]
    x_axis: tuple[Coordinate, Coordinate, Coordinate]

    @model_validator(mode="after")
    def orthogonal_axes(self) -> "SketchFrame":
        n = math.sqrt(sum(v * v for v in self.normal))
        x = math.sqrt(sum(v * v for v in self.x_axis))
        if n == 0 or x == 0 or abs(sum(a*b for a, b in zip(self.normal, self.x_axis))) > 1e-9*n*x:
            raise ValueError("frame requires nonzero orthogonal normal and x_axis")
        return self


class InstancePlacementArgs(FrozenContract):
    object: ObjectName
    translation_mm: tuple[Coordinate, Coordinate, Coordinate]
    rotation_axis: tuple[Coordinate, Coordinate, Coordinate] = (0, 0, 1)
    rotation_deg: float = Field(default=0, ge=-360, le=360)

    @model_validator(mode='after')
    def nonzero_axis(self):
        if sum(v * v for v in self.rotation_axis) < 1e-12:
            raise ValueError('rotation axis cannot be zero')
        return self


class InstanceCreateArgs(InstancePlacementArgs):
    source: ObjectName


class LineGeometry(FrozenContract):
    kind: Literal["line"]
    start: Point2D
    end: Point2D
    construction: bool = False

    @model_validator(mode="after")
    def non_zero_length(self) -> "LineGeometry":
        if self.start == self.end:
            raise ValueError("line geometry must have non-zero length")
        return self


class CircleGeometry(FrozenContract):
    kind: Literal["circle"]
    center: Point2D
    radius_mm: Millimetres
    construction: bool = False


class RectangleGeometry(FrozenContract):
    kind: Literal["rectangle"]
    corner: Point2D
    width_mm: Millimetres
    height_mm: Millimetres
    construction: bool = False


class ArcGeometry(FrozenContract):
    kind: Literal["arc"]
    center: Point2D
    radius_mm: Millimetres
    start_angle_deg: float = Field(allow_inf_nan=False)
    end_angle_deg: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def arc_sweep(self) -> "ArcGeometry":
        if not 0 < self.end_angle_deg-self.start_angle_deg < 360:
            raise ValueError("arc must have an explicit counterclockwise sweep between zero and 360 degrees")
        return self


class RegularPolygonGeometry(FrozenContract):
    kind: Literal["regular_polygon"]
    center: Point2D
    radius_mm: Millimetres
    sides: int = Field(ge=3, strict=True)
    rotation_deg: float = Field(default=0, allow_inf_nan=False)


SketchGeometry = Annotated[
    LineGeometry | CircleGeometry | RectangleGeometry,
    Field(discriminator="kind"),
]


class ConstraintReference(FrozenContract):
    geometry_index: int = Field(ge=0, le=100_000)
    point_position: Literal[1, 2, 3] | None = None


class DocumentInspectArgs(FrozenContract):
    pass


class FreeCADAPIArgs(FrozenContract):
    """Native API program, run in a separate isolated FreeCAD process."""
    source: str = Field(min_length=1)
    environment: Literal["headless", "gui"] = "headless"
    modules: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_program(self):
        import ast
        import re
        try:
            ast.parse(self.source, mode="exec")
        except SyntaxError as exc:
            raise ValueError(f"API program syntax error at line {exc.lineno}: {exc.msg}") from exc
        if any(not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", m) for m in self.modules):
            raise ValueError("module names must be Python import paths")
        return self


class SketchCreateArgs(FrozenContract):
    name: ObjectName
    body: ObjectName = "Body"
    plane: Literal["xy", "xz", "yz"] = "xy"
    offset_mm: Coordinate = 0.0
    reversed: bool = False
    frame: SketchFrame | None = None

    @model_validator(mode="after")
    def one_placement(self) -> "SketchCreateArgs":
        if self.frame is not None and (self.plane != "xy" or self.offset_mm != 0 or self.reversed):
            raise ValueError("explicit frame cannot be combined with legacy plane/offset/reversed")
        return self


class SketchAddGeometryArgs(FrozenContract):
    sketch: ObjectName
    geometry: SketchGeometry


class SketchAddProfileArgs(FrozenContract):
    sketch: ObjectName
    geometry: Annotated[CircleGeometry | RectangleGeometry | ArcGeometry | RegularPolygonGeometry, Field(discriminator="kind")]


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
    value_mm: Coordinate | None = None

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
        if self.kind in {"distance", "radius", "diameter"} and self.value_mm is not None and self.value_mm <= 0:
            raise ValueError(f"{self.kind} requires a positive physical length")
        requires_second = self.kind in {"coincident", "equal"}
        if requires_second != (self.second is not None):
            raise ValueError(f"{self.kind} second reference requirement is not satisfied")
        if self.kind in {"distance_x", "distance_y", "coincident"}:
            references = (self.first,) if self.second is None else (self.first, self.second)
            if any(reference.point_position is None for reference in references):
                raise ValueError(f"{self.kind} requires point_position")
        return self


class SketchSetConstraintArgs(FrozenContract):
    sketch: ObjectName
    constraint_index: int = Field(ge=0, le=100_000, strict=True)
    expected_type: Literal['DistanceX','DistanceY','Distance','Radius','Diameter','Angle']
    value_mm: Coordinate | None = None
    value_deg: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode='after')
    def dimension_value(self):
        if self.expected_type == 'Angle':
            if self.value_deg is None or self.value_mm is not None:
                raise ValueError('angle edits require value_deg only')
            return self
        if self.value_mm is None or self.value_deg is not None:
            raise ValueError('length edits require value_mm only')
        if self.expected_type in {'Distance','Radius','Diameter'} and self.value_mm <= 0:
            raise ValueError('length, radius and diameter must be positive')
        return self


class FeaturePadArgs(FrozenContract):
    name: ObjectName
    profile: ObjectName
    length_mm: Millimetres
    reversed: bool = False


class FeatureLoftArgs(FrozenContract):
    name: ObjectName
    profiles: tuple[ObjectName, ...] = Field(min_length=2)
    subtractive: bool = False
    ruled: bool = False

    @model_validator(mode="after")
    def distinct_sections(self) -> "FeatureLoftArgs":
        if len(set(self.profiles)) != len(self.profiles):
            raise ValueError("loft sections must reference distinct profiles")
        return self


class FeatureSweepArgs(FrozenContract):
    name: ObjectName
    profile: ObjectName
    path: ObjectName
    subtractive: bool = False

    @model_validator(mode="after")
    def separate_profile_and_path(self) -> "FeatureSweepArgs":
        if self.profile == self.path:
            raise ValueError("sweep profile and path must be distinct")
        return self


class FeatureRevolveArgs(FrozenContract):
    name: ObjectName
    profile: ObjectName
    axis: Literal["x", "y", "z"]
    angle_deg: float = Field(gt=0, le=360, allow_inf_nan=False)
    reversed: bool = False
    subtractive: bool = False


class FeaturePolarPatternArgs(FrozenContract):
    name: ObjectName
    originals: tuple[ObjectName, ...] = Field(min_length=1)
    axis: Literal["x", "y", "z"]
    occurrences: int = Field(ge=2, strict=True)
    angle_deg: float = Field(gt=0, le=360, allow_inf_nan=False)
    reversed: bool = False


class FeatureLinearPatternArgs(FrozenContract):
    name: ObjectName
    originals: tuple[ObjectName, ...] = Field(min_length=1)
    axis: Literal["x", "y", "z"]
    occurrences: int = Field(ge=2, strict=True)
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


class Counterbore(FrozenContract):
    kind: Literal["counterbore"]
    diameter_mm: Millimetres
    depth_mm: Millimetres


class Countersink(FrozenContract):
    kind: Literal["countersink"]
    diameter_mm: Millimetres
    angle_deg: float = Field(gt=0, lt=180, allow_inf_nan=False)


class FeatureHoleArgs(FrozenContract):
    name: ObjectName
    profile: ObjectName
    diameter_mm: Millimetres
    depth_mm: Millimetres | None = None
    through_all: bool = False
    reversed: bool = False
    cut: Annotated[Counterbore | Countersink, Field(discriminator="kind")] | None = None

    @model_validator(mode="after")
    def depth_or_through_all(self) -> "FeatureHoleArgs":
        if self.through_all == (self.depth_mm is not None):
            raise ValueError("hole requires exactly one of depth_mm or through_all")
        if self.cut is not None:
            if self.cut.diameter_mm <= self.diameter_mm:
                raise ValueError("hole entrance must be wider than its shaft")
            entrance_depth = (self.cut.depth_mm if isinstance(self.cut, Counterbore) else
                (self.cut.diameter_mm-self.diameter_mm)/(2*math.tan(math.radians(self.cut.angle_deg/2))))
            if self.depth_mm is not None and entrance_depth >= self.depth_mm:
                raise ValueError("hole entrance must leave a positive shaft depth")
        return self


def _dressup_selection_schema(schema, alternatives):
    """Publish mutually exclusive modes, including the legacy all-edges default."""
    modes=['use_all_edges',*alternatives]
    variants=[]
    for selected in modes:
        properties={key:({'const':False} if key=='use_all_edges' else {'type':'null'})
                    for key in modes if key!=selected}
        properties[selected]={'const':True} if selected=='use_all_edges' else {'not':{'type':'null'}}
        variants.append({'properties':properties,'required':([] if selected=='use_all_edges'
                        else ['use_all_edges',selected])})
    schema['oneOf']=variants
    return schema


class FeatureFilletArgs(FrozenContract):
    name: ObjectName
    target: ObjectName
    radius_mm: Millimetres
    use_all_edges: bool = True
    selector: FreeCADTopologySelector | None = None

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        return _dressup_selection_schema(handler.resolve_ref_schema(handler(core_schema)), ['selector'])

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
    edge_scope: Literal["outer", "hole_mouths", "all"] | None = None

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        return _dressup_selection_schema(handler.resolve_ref_schema(handler(core_schema)), ['selector','edge_scope'])

    @model_validator(mode="after")
    def selection_mode(self) -> "FeatureChamferArgs":
        if sum((self.use_all_edges, self.selector is not None, self.edge_scope is not None)) != 1:
            raise ValueError("chamfer requires exactly one of use_all_edges, selector or edge_scope")
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
    objects: tuple[ObjectName, ...] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def unique_formats(self) -> "DocumentExportArgs":
        if self.objects is not None and len(set(self.objects)) != len(self.objects):
            raise ValueError('export objects must be unique')
        if not self.formats or len(self.formats) != len(set(self.formats)):
            raise ValueError("document export formats must be non-empty and unique")
        if "fcstd" not in self.formats:
            raise ValueError("FreeCAD executions must retain an fcstd artifact")
        return self


OperationArgs = (
    DocumentInspectArgs
    | FreeCADAPIArgs
    | SketchCreateArgs
    | SketchAddGeometryArgs
    | SketchAddProfileArgs
    | SketchAddConstraintArgs
    | SketchSetConstraintArgs
    | FeaturePadArgs
    | FeatureLoftArgs
    | FeatureSweepArgs
    | FeatureRevolveArgs
    | FeaturePolarPatternArgs
    | FeatureLinearPatternArgs
    | FeaturePocketArgs
    | FeatureHoleArgs
    | FeatureFilletArgs
    | FeatureChamferArgs
    | PropertySetArgs
    | DocumentExportArgs
    | InstanceCreateArgs
    | InstancePlacementArgs
)


_ACTION_ARG_TYPES: dict[str, type[FrozenContract]] = {
    "document.inspect": DocumentInspectArgs,
    "api.execute": FreeCADAPIArgs,
    "sketch.create": SketchCreateArgs,
    "sketch.add_geometry": SketchAddGeometryArgs,
    "sketch.add_profile": SketchAddProfileArgs,
    "sketch.add_constraint": SketchAddConstraintArgs,
    "sketch.set_constraint": SketchSetConstraintArgs,
    "feature.pad": FeaturePadArgs,
    "feature.loft": FeatureLoftArgs,
    "feature.sweep": FeatureSweepArgs,
    "feature.revolve": FeatureRevolveArgs,
    "feature.polar_pattern": FeaturePolarPatternArgs,
    "feature.linear_pattern": FeatureLinearPatternArgs,
    "feature.pocket": FeaturePocketArgs,
    "feature.hole": FeatureHoleArgs,
    "feature.fillet": FeatureFilletArgs,
    "feature.chamfer": FeatureChamferArgs,
    "property.set": PropertySetArgs,
    "document.export": DocumentExportArgs,
    "assembly.instance": InstanceCreateArgs,
    "assembly.place": InstancePlacementArgs,
}


class FreeCADOperation(FrozenContract):
    op_id: OperationId
    action: Literal[
        "document.inspect",
        "api.execute",
        "sketch.create",
        "sketch.add_geometry",
        "sketch.add_profile",
        "sketch.add_constraint",
        "sketch.set_constraint",
        "feature.pad",
        "feature.loft",
        "feature.sweep",
        "feature.revolve",
        "feature.polar_pattern",
        "feature.linear_pattern",
        "feature.pocket",
        "feature.hole",
        "feature.fillet",
        "feature.chamfer",
        "property.set",
        "document.export",
        "assembly.instance",
        "assembly.place",
    ]
    args: dict

    @model_validator(mode="after")
    def validate_args(self) -> "FreeCADOperation":
        validated = _ACTION_ARG_TYPES[self.action].model_validate(self.args)
        payload = validated.model_dump(mode="json")
        if self.action == "sketch.create" and payload.get("frame") is None:
            payload.pop("frame", None)
        if self.action == "feature.hole" and payload.get("cut") is None:
            payload.pop("cut", None)
        if self.action == "sketch.set_constraint":
            payload = {k:v for k,v in payload.items() if v is not None}
        # Additive scope support must not change hashes of retained v1 plans.
        if self.action == "feature.chamfer" and payload.get("edge_scope") is None:
            payload.pop("edge_scope", None)
        if self.action == 'document.export' and payload.get('objects') is None:
            payload.pop('objects',None)
        object.__setattr__(self, "args", payload)
        return self

    def typed_args(self) -> OperationArgs:
        return _ACTION_ARG_TYPES[self.action].model_validate(self.args)


class FreeCADOperationPlan(FrozenContract):
    schema_version: Literal["freecad-operation-plan.v1"] = (
        "freecad-operation-plan.v1"
    )
    document_name: ObjectName = "Model"
    execution_mode: Literal["final", "checkpoint"] = "final"
    operations: tuple[FreeCADOperation, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_sequence(self) -> "FreeCADOperationPlan":
        if self.execution_mode == "checkpoint" and not any(
            op.action not in {"document.inspect", "document.export"} for op in self.operations
        ):
            raise ValueError("a checkpoint requires a document-changing operation")
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
        if any(op.action == "api.execute" for op in self.operations):
            if tuple(op.action for op in self.operations) != ("api.execute", "document.export"):
                raise ValueError("API plans require exactly api.execute followed by document.export")
            if not self.operations[-1].args.get('objects'):
                raise ValueError('API plans must explicitly select final export objects')
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


def normalize_capability_schema(value):
    """Keep the same contract identity across equivalent Pydantic Literal schemas."""
    if isinstance(value, list):
        return [normalize_capability_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: normalize_capability_schema(item) for key, item in value.items()}
    if 'const' in result and result.get('enum') == [result['const']]:
        result.pop('enum')
    return result


def capability_schemas() -> dict:
    return {action: normalize_capability_schema(model.model_json_schema())
            for action, model in _ACTION_ARG_TYPES.items()}


def planner_discriminated_shapes() -> dict:
    """Small wire signatures from the same typed schemas used by validation.

    A discriminator is required even when only one variant's other fields fit.
    Advertising exact tags avoids inferring a geometry type after generation.
    """
    result = {}
    for action, schema in capability_schemas().items():
        fields = {}
        for field, definition in schema['properties'].items():
            choices = [definition, *definition.get('anyOf', [])]
            for choice in choices:
                discriminator = choice.get('discriminator')
                if not discriminator:
                    continue
                variants = {}
                for tag, reference in discriminator['mapping'].items():
                    shape = schema['$defs'][reference.rsplit('/', 1)[-1]]
                    required = shape.get('required', [])
                    variants[tag] = {'required': required,
                        'optional': [key for key in shape['properties'] if key not in required]}
                fields[field] = {'tag': discriminator['propertyName'], 'variants': variants}
        if fields:
            result[action] = fields
    return result


def capability_contract_sha256() -> str:
    """Identity shared by the host planner and the packaged native executor."""
    import hashlib
    from pathlib import Path
    return hashlib.sha256(Path(__file__).with_name('capabilities.json').read_bytes()).hexdigest()


class FreeCADPlanningError(ValueError):
    """A classified planner outcome, distinct from an invalid JSON contract."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def decode_planner_response(content: str) -> dict:
    import json
    value = json.loads(content)
    if isinstance(value, dict) and value.get('error'):
        reason = str(value['error'])
        code = ('engineering_input_required' if reason.startswith('needs_clarification:')
                else 'freecad_capability_unsupported')
        raise FreeCADPlanningError(code, reason) from ValueError(reason)
    return value
