"""Allowlisted FreeCAD mutation boundary executed with Debian's Python ABI.

This module intentionally uses only the standard library plus FreeCAD modules.
The surrounding sandbox dispatcher runs on the product Python runtime and
starts this file with ``/usr/bin/python3`` so the FreeCAD 3.11 bindings are
never imported into the product's Python 3.12 process.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import traceback
from pathlib import Path
from typing import Any

import FreeCAD as App
import Import
import Part
import Sketcher

from freecad_state_projector import project_saved_document
from freecad_topology import TopologyResolutionError, resolve_topology_selector
from freecad_bom import BOMError, run_bom
from freecad_scene import component_shapes, run_scene
from freecad_engineering import EngineeringError, run_engineering
from freecad_result_channel import publish_result
from freecad_sketch_diagnostics import diagnose_sketch
from freecad_edge_scope import EdgeScopeError, resolve_edge_scope, verify_protected_faces
from freecad_api import execute_program


INPUT_ROOT = Path("/sandbox/input")
OUTPUT_ROOT = Path("/sandbox/output")
LEDGER_NAME = "CADAgentLedger"
NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
ACTION_KEYS: dict[str, tuple[set[str], set[str]]] = {
    "document.inspect": (set(), set()),
    "api.execute": ({"source", "environment", "modules"}, {"source"}),
    "sketch.create": (
        {"name", "body", "plane", "offset_mm", "reversed", "frame"},
        {"name"},
    ),
    "sketch.add_geometry": ({"sketch", "geometry"}, {"sketch", "geometry"}),
    "sketch.add_profile": ({"sketch", "geometry"}, {"sketch", "geometry"}),
    "sketch.add_constraint": (
        {"sketch", "kind", "first", "second", "value_mm"},
        {"sketch", "kind", "first"},
    ),
    "sketch.set_constraint": ({"sketch","constraint_index","expected_type","value_mm","value_deg"}, {"sketch","constraint_index","expected_type"}),
    "feature.pad": ({"name", "profile", "length_mm", "reversed"}, {"name", "profile", "length_mm"}),
    "feature.loft": ({"name", "profiles", "subtractive", "ruled"}, {"name", "profiles"}),
    "feature.sweep": ({"name", "profile", "path", "subtractive"}, {"name", "profile", "path"}),
    "feature.revolve": ({"name", "profile", "axis", "angle_deg", "reversed", "subtractive"}, {"name", "profile", "axis", "angle_deg"}),
    "feature.polar_pattern": ({"name", "originals", "axis", "occurrences", "angle_deg", "reversed"}, {"name", "originals", "axis", "occurrences", "angle_deg"}),
    "feature.linear_pattern": ({"name", "originals", "axis", "occurrences", "length_mm", "reversed"}, {"name", "originals", "axis", "occurrences", "length_mm"}),
    "feature.pocket": (
        {"name", "profile", "length_mm", "through_all", "reversed"},
        {"name", "profile"},
    ),
    "feature.hole": (
        {"name", "profile", "diameter_mm", "depth_mm", "through_all", "reversed", "cut"},
        {"name", "profile", "diameter_mm"},
    ),
    "feature.fillet": (
        {"name", "target", "radius_mm", "use_all_edges", "selector"},
        {"name", "target", "radius_mm"},
    ),
    "feature.chamfer": (
        {"name", "target", "size_mm", "use_all_edges", "selector", "edge_scope"},
        {"name", "target", "size_mm"},
    ),
    "property.set": (
        {"object", "property", "value", "expected_property_type", "unit"},
        {"object", "property", "value"},
    ),
    "document.export": ({"formats", "basename", "objects"}, {"formats"}),
    "assembly.instance": ({"object", "source", "translation_mm", "rotation_axis", "rotation_deg"}, {"object", "source", "translation_mm"}),
    "assembly.place": ({"object", "translation_mm", "rotation_axis", "rotation_deg"}, {"object", "translation_mm"}),
}


class FreeCADRunnerError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        op_id: str | None = None,
        action: str | None = None,
        details: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.op_id = op_id
        self.action = action
        self.details = details or {}


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or NAME_PATTERN.fullmatch(value) is None:
        raise FreeCADRunnerError("invalid_name", f"{label} is not a safe object name")
    return value


def _number(
    value: object,
    label: str,
    *,
    minimum: float = -1_000_000,
    maximum: float = 1_000_000,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FreeCADRunnerError("invalid_number", f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise FreeCADRunnerError("invalid_number", f"{label} is outside the supported range")
    if positive and result <= 0:
        raise FreeCADRunnerError("invalid_number", f"{label} must be positive")
    return result


def _keys(action: str, args: object) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise FreeCADRunnerError("invalid_operation_args", f"{action} args must be an object")
    allowed, required = ACTION_KEYS[action]
    unknown = sorted(set(args) - allowed)
    missing = sorted(required - set(args))
    if unknown:
        raise FreeCADRunnerError("invalid_operation_args", f"unsupported {action} args: {', '.join(unknown)}")
    if missing:
        raise FreeCADRunnerError("invalid_operation_args", f"missing {action} args: {', '.join(missing)}")
    return args


def _object(document: Any, value: object, label: str) -> Any:
    name = _name(value, label)
    obj = document.getObject(name)
    if obj is None:
        raise FreeCADRunnerError("object_not_found", f"{label} does not exist: {name}")
    return obj


def _body_for(document: Any, obj: Any) -> Any:
    for candidate in document.Objects:
        if getattr(candidate, "TypeId", "") != "PartDesign::Body":
            continue
        if obj in getattr(candidate, "Group", ()):
            return candidate
    body = document.getObject("Body")
    if body is None or getattr(body, "TypeId", "") != "PartDesign::Body":
        raise FreeCADRunnerError("body_not_found", f"no PartDesign body contains {obj.Name}")
    return body


def _ledger(document: Any) -> Any:
    ledger = document.getObject(LEDGER_NAME)
    if ledger is None:
        ledger = document.addObject("App::FeaturePython", LEDGER_NAME)
        ledger.addProperty("App::PropertyStringList", "OperationRecords", "CAD Agent")
        ledger.OperationRecords = []
    return ledger


def _operation_digest(operation: dict[str, Any]) -> str:
    encoded = json.dumps(
        operation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ledger_records(ledger: Any) -> dict[str, str]:
    records: dict[str, str] = {}
    for item in getattr(ledger, "OperationRecords", ()):
        op_id, separator, digest = str(item).partition(":")
        if separator and op_id and len(digest) == 64:
            records[op_id] = digest
    return records


def _point(value: object, label: str) -> tuple[float, float]:
    if not isinstance(value, dict) or set(value) != {"x", "y"}:
        raise FreeCADRunnerError("invalid_geometry", f"{label} must contain x and y")
    return _number(value["x"], f"{label}.x"), _number(value["y"], f"{label}.y")


def _reference(value: object, label: str) -> tuple[int, int | None]:
    if not isinstance(value, dict) or not set(value) <= {"geometry_index", "point_position"}:
        raise FreeCADRunnerError("invalid_constraint", f"{label} is invalid")
    index = value.get("geometry_index")
    point = value.get("point_position")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 100_000:
        raise FreeCADRunnerError("invalid_constraint", f"{label}.geometry_index is invalid")
    if point is not None and point not in {1, 2, 3}:
        raise FreeCADRunnerError("invalid_constraint", f"{label}.point_position is invalid")
    return index, point


def _sketch_create(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _name(args["name"], "sketch name")
    if document.getObject(name) is not None:
        raise FreeCADRunnerError("object_name_conflict", f"object already exists: {name}")
    body_name = _name(args.get("body", "Body"), "body name")
    body = document.getObject(body_name)
    if body is None:
        body = document.addObject("PartDesign::Body", body_name)
    if getattr(body, "TypeId", "") != "PartDesign::Body":
        raise FreeCADRunnerError("object_type_mismatch", f"{body_name} is not a PartDesign body")
    plane = str(args.get("plane", "xy"))
    plane_name = {"xy": "XY_Plane", "xz": "XZ_Plane", "yz": "YZ_Plane"}.get(plane)
    if plane_name is None:
        raise FreeCADRunnerError("invalid_plane", f"unsupported sketch plane: {plane}")
    sketch = document.addObject("Sketcher::SketchObject", name)
    body.addObject(sketch)
    frame = args.get("frame")
    if frame is not None:
        if args.get("plane", "xy") != "xy" or args.get("offset_mm", 0) != 0 or args.get("reversed", False):
            raise FreeCADRunnerError("invalid_frame", "explicit frame conflicts with legacy placement")
        if not isinstance(frame, dict) or set(frame) - {"schema_version", "origin", "normal", "x_axis"} or frame.get("schema_version", "body-frame.v1") != "body-frame.v1":
            raise FreeCADRunnerError("invalid_frame", "unsupported explicit sketch frame")
        origin = frame.get("origin")
        if not isinstance(origin, dict) or set(origin) != {"x", "y", "z"}:
            raise FreeCADRunnerError("invalid_frame", "frame origin requires x, y, z")
        def axis(key):
            values = frame.get(key)
            if not isinstance(values, (list, tuple)) or len(values) != 3:
                raise FreeCADRunnerError("invalid_frame", f"frame {key} requires three numbers")
            vector = App.Vector(*(_number(v, key) for v in values))
            if vector.Length == 0:
                raise FreeCADRunnerError("invalid_frame", f"frame {key} cannot be zero")
            return vector / vector.Length
        normal, x_axis = axis("normal"), axis("x_axis")
        if abs(normal.dot(x_axis)) > 1e-9:
            raise FreeCADRunnerError("invalid_frame", "frame axes must be orthogonal")
        rotation = App.Rotation(x_axis, normal.cross(x_axis), normal, "ZXY")
        sketch.Placement = App.Placement(App.Vector(*(_number(origin[k], f"origin.{k}") for k in ("x", "y", "z"))), rotation)
        sketch.addProperty("App::PropertyString", "CADAgentPlacementSchema")
        sketch.CADAgentPlacementSchema = "body-frame.v1"
        sketch.setEditorMode("CADAgentPlacementSchema", 1)
        return {"object": sketch.Name, "type_id": sketch.TypeId}
    # Retained v1 operation payloads deliberately retain their original semantics.
    # New side/rotated sketches use an explicit Body frame instead of reinterpreting
    # AttachmentOffset values from historical documents.
    sketch.AttachmentSupport = (document.getObject(plane_name), [""])
    sketch.MapMode = "FlatFace"
    sketch.MapReversed = bool(args.get("reversed", False))
    offset = _number(args.get("offset_mm", 0.0), "offset_mm")
    offset_vector = {
        "xy": App.Vector(0, 0, offset),
        "xz": App.Vector(0, offset, 0),
        "yz": App.Vector(offset, 0, 0),
    }[plane]
    sketch.AttachmentOffset = App.Placement(offset_vector, App.Rotation())
    return {"object": sketch.Name, "type_id": sketch.TypeId}


def _sketch_add_geometry(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    sketch = _object(document, args["sketch"], "sketch")
    if getattr(sketch, "TypeId", "") != "Sketcher::SketchObject":
        raise FreeCADRunnerError("object_type_mismatch", f"{sketch.Name} is not a Sketch")
    geometry = args["geometry"]
    if not isinstance(geometry, dict):
        raise FreeCADRunnerError("invalid_geometry", "geometry must be an object")
    kind = geometry.get("kind")
    construction = bool(geometry.get("construction", False))
    indexes: list[int] = []
    if kind == "line":
        start = _point(geometry.get("start"), "line.start")
        end = _point(geometry.get("end"), "line.end")
        if start == end:
            raise FreeCADRunnerError("invalid_geometry", "line must have non-zero length")
        indexes.append(
            int(
                sketch.addGeometry(
                    Part.LineSegment(App.Vector(*start, 0), App.Vector(*end, 0)),
                    construction,
                )
            )
        )
    elif kind == "circle":
        center = _point(geometry.get("center"), "circle.center")
        radius = _number(geometry.get("radius_mm"), "radius_mm", positive=True)
        indexes.append(
            int(
                sketch.addGeometry(
                    Part.Circle(App.Vector(*center, 0), App.Vector(0, 0, 1), radius),
                    construction,
                )
            )
        )
    elif kind == "rectangle":
        x, y = _point(geometry.get("corner"), "rectangle.corner")
        width = _number(geometry.get("width_mm"), "width_mm", positive=True)
        height = _number(geometry.get("height_mm"), "height_mm", positive=True)
        points = (
            (x, y),
            (x + width, y),
            (x + width, y + height),
            (x, y + height),
        )
        lines = [
            Part.LineSegment(
                App.Vector(*points[index], 0),
                App.Vector(*points[(index + 1) % 4], 0),
            )
            for index in range(4)
        ]
        created = sketch.addGeometry(lines, construction)
        indexes.extend(int(value) for value in created)
    else:
        raise FreeCADRunnerError("invalid_geometry", f"unsupported geometry kind: {kind}")
    return {"object": sketch.Name, "geometry_indexes": indexes}


def _sketch_add_curve_profile(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    geometry = args["geometry"]
    sketch = _object(document, args["sketch"], "sketch")
    center = App.Vector(*_point(geometry["center"], "center"), 0)
    radius = _number(geometry["radius_mm"], "radius_mm", positive=True)
    circle = Part.Circle(center, App.Vector(0, 0, 1), radius)
    kind = geometry["kind"]
    if kind == "arc":
        start = _number(geometry["start_angle_deg"], "start_angle_deg")
        end = _number(geometry["end_angle_deg"], "end_angle_deg")
        if not 0 < end-start < 360:
            raise FreeCADRunnerError("invalid_geometry", "arc sweep must be between zero and 360 degrees")
        base = sketch.addGeometry(Part.ArcOfCircle(circle, math.radians(start), math.radians(end)), False)
        indexes = [base]
        radial_targets = [(base, 1, start), (base, 2, end)]
    else:
        sides = geometry["sides"]
        if not isinstance(sides, int) or isinstance(sides, bool) or sides < 3:
            raise FreeCADRunnerError("invalid_geometry", "polygon requires at least three sides")
        angle = _number(geometry.get("rotation_deg", 0), "rotation_deg")
        base = sketch.addGeometry(circle, True)
        points = [center + App.Vector(radius*math.cos(math.radians(angle)+i*2*math.pi/sides),
                   radius*math.sin(math.radians(angle)+i*2*math.pi/sides), 0) for i in range(sides)]
        indexes = list(sketch.addGeometry([Part.LineSegment(points[i], points[(i+1)%sides]) for i in range(sides)], False))
        for i, index in enumerate(indexes):
            sketch.addConstraint(Sketcher.Constraint('Coincident', index, 2, indexes[(i+1)%sides], 1))
            sketch.addConstraint(Sketcher.Constraint('PointOnObject', index, 1, base))
            if i:
                sketch.addConstraint(Sketcher.Constraint('Equal', indexes[0], index))
        radial_targets = [(indexes[0], 1, angle)]
    sketch.addConstraint(Sketcher.Constraint('DistanceX', base, 3, center.x))
    sketch.addConstraint(Sketcher.Constraint('DistanceY', base, 3, center.y))
    sketch.addConstraint(Sketcher.Constraint('Radius', base, radius))
    for target, point, angle in radial_targets:
        endpoint = center + App.Vector(radius*math.cos(math.radians(angle)), radius*math.sin(math.radians(angle)), 0)
        radial = sketch.addGeometry(Part.LineSegment(center, endpoint), True)
        sketch.addConstraint(Sketcher.Constraint('Coincident', radial, 1, base, 3))
        sketch.addConstraint(Sketcher.Constraint('Coincident', radial, 2, target, point))
        sketch.addConstraint(Sketcher.Constraint('Angle', radial, math.radians(angle) % (2*math.pi)))
    return {"object": sketch.Name, "geometry_indexes": indexes}


def _sketch_add_profile(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Expand fully specified primitives into native editable dimensions.

    Indexes come from the actual sketch; existing geometry is never renumbered.
    Each degree of freedom is constrained exactly once, including signed origins.
    """
    geometry = args["geometry"]
    if geometry.get("kind") in {"arc", "regular_polygon"}:
        return _sketch_add_curve_profile(document, args)
    if geometry.get("kind") not in {"circle", "rectangle"}:
        raise FreeCADRunnerError("invalid_geometry", "profile requires circle or rectangle")
    result = _sketch_add_geometry(document, args)
    indexes = result["geometry_indexes"]
    sketch = args["sketch"]
    def constrain(kind, index, value=None, point=None, second=None):
        return _sketch_add_constraint(document, {"sketch": sketch, "kind": kind,
            "first": {"geometry_index": index, "point_position": point},
            "value_mm": value, "second": second})
    if geometry["kind"] == "circle":
        index = indexes[0]
        for kind, axis in (("distance_x", "x"), ("distance_y", "y")):
            constrain(kind, index, geometry["center"][axis], 3)
        constrain("radius", index, geometry["radius_mm"])
    else:
        for position, index in enumerate(indexes):
            constrain("horizontal" if position % 2 == 0 else "vertical", index)
            constrain("coincident", index, point=2,
                      second={"geometry_index": indexes[(position + 1) % 4], "point_position": 1})
        constrain("distance", indexes[0], geometry["width_mm"])
        constrain("distance", indexes[1], geometry["height_mm"])
        for kind, axis in (("distance_x", "x"), ("distance_y", "y")):
            constrain(kind, indexes[0], geometry["corner"][axis], 1)
    return result


def _sketch_add_constraint(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    sketch = _object(document, args["sketch"], "sketch")
    if getattr(sketch, "TypeId", "") != "Sketcher::SketchObject":
        raise FreeCADRunnerError("object_type_mismatch", f"{sketch.Name} is not a Sketch")
    kind = str(args["kind"])
    first_index, first_point = _reference(args["first"], "first")
    second = args.get("second")
    second_index: int | None = None
    second_point: int | None = None
    if second is not None:
        second_index, second_point = _reference(second, "second")
    value = args.get("value_mm")
    constraint: Any
    if kind in {"horizontal", "vertical"}:
        constraint = Sketcher.Constraint(kind.title(), first_index)
    elif kind in {"radius", "diameter"}:
        constraint = Sketcher.Constraint(
            kind.title(),
            first_index,
            _number(value, "value_mm", positive=True),
        )
    elif kind == "distance":
        constraint = Sketcher.Constraint(
            "Distance",
            first_index,
            _number(value, "value_mm", positive=True),
        )
    elif kind in {"distance_x", "distance_y"}:
        if first_point is None:
            raise FreeCADRunnerError("sketch_malformed_constraint", f"{kind} requires first point_position")
        freecad_kind = "DistanceX" if kind == "distance_x" else "DistanceY"
        amount = _number(value, "value_mm")
        if second_index is None:
            constraint = Sketcher.Constraint(freecad_kind, first_index, first_point, amount)
        else:
            if second_point is None:
                raise FreeCADRunnerError("sketch_malformed_constraint", f"{kind} requires second point_position")
            constraint = Sketcher.Constraint(
                freecad_kind,
                first_index,
                first_point,
                second_index,
                second_point,
                amount,
            )
    elif kind == "coincident":
        if first_point is None or second_index is None or second_point is None:
            raise FreeCADRunnerError("sketch_malformed_constraint", "coincident requires two points")
        constraint = Sketcher.Constraint(
            "Coincident", first_index, first_point, second_index, second_point
        )
    elif kind == "equal":
        if second_index is None:
            raise FreeCADRunnerError("sketch_malformed_constraint", "equal requires two geometries")
        constraint = Sketcher.Constraint("Equal", first_index, second_index)
    else:
        raise FreeCADRunnerError("sketch_malformed_constraint", f"unsupported constraint kind: {kind}")
    try:
        index = int(sketch.addConstraint(constraint))
    except (IndexError, ValueError, RuntimeError) as exc:
        raise FreeCADRunnerError("sketch_malformed_constraint", str(exc)) from exc
    return {"object": sketch.Name, "constraint_index": index}


def _sketch_set_constraint(document, args):
    sketch = _object(document, args['sketch'], 'sketch')
    if sketch.TypeId != 'Sketcher::SketchObject':
        raise FreeCADRunnerError('object_type_mismatch', 'constraint target must be a Sketch')
    index = args['constraint_index']
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < sketch.ConstraintCount:
        raise FreeCADRunnerError('sketch_constraint_missing', 'constraint index is not present in the current sketch')
    kind = args['expected_type']
    if kind not in {'DistanceX','DistanceY','Distance','Radius','Diameter','Angle'} or sketch.Constraints[index].Type != kind:
        raise FreeCADRunnerError('sketch_constraint_type_mismatch', 'constraint type does not match the immutable edit')
    if not sketch.getDriving(index):
        raise FreeCADRunnerError('sketch_reference_constraint', 'reference dimensions are measured, not editable')
    field, unit = ('value_deg','deg') if kind == 'Angle' else ('value_mm','mm')
    other = 'value_mm' if kind == 'Angle' else 'value_deg'
    if field not in args or args.get(other) is not None:
        raise FreeCADRunnerError('sketch_constraint_unit_mismatch', 'constraint edit has incorrect units')
    value = _number(args[field], field, positive=kind in {'Distance','Radius','Diameter'})
    try:
        sketch.setDatum(index, App.Units.Quantity(f'{value} {unit}'))
    except (IndexError,ValueError,RuntimeError) as exc:
        raise FreeCADRunnerError('sketch_constraint_update_failed', str(exc)) from exc
    return {'object':sketch.Name, 'constraint_index':index, field:value}


def _require_fully_constrained(profile: Any) -> None:
    _validate_sketch(profile, require_fully_constrained=True)


def _validate_sketch(sketch: Any, *, require_fully_constrained: bool = False,
                     op_id: str | None = None, action: str | None = None) -> dict[str, Any]:
    diagnosis = diagnose_sketch(sketch)
    state = diagnosis['constraint_status']
    code = {
        'redundant': 'sketch_redundant_constraints',
        'conflicting': 'sketch_conflicting_constraints',
        'invalid': 'sketch_solver_failed',
        **({'under_constrained': 'sketch_under_constrained'} if require_fully_constrained else {}),
    }.get(state)
    if code:
        raise FreeCADRunnerError(
            code, f"Sketch {sketch.Name}: {state}; {diagnosis['native_status']}"[:4000],
            op_id=op_id, action=action, details=diagnosis,
        )
    return diagnosis


def _feature_pad(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _name(args["name"], "pad name")
    if document.getObject(name) is not None:
        raise FreeCADRunnerError("object_name_conflict", f"object already exists: {name}")
    profile = _object(document, args["profile"], "profile")
    _require_fully_constrained(profile)
    body = _body_for(document, profile)
    feature = document.addObject("PartDesign::Pad", name)
    body.addObject(feature)
    feature.Profile = profile
    feature.Length = _number(args["length_mm"], "length_mm", positive=True)
    feature.Reversed = bool(args.get("reversed", False))
    return {"object": feature.Name, "type_id": feature.TypeId}


def _feature_loft(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _name(args["name"], "loft name")
    names = args.get("profiles")
    if not isinstance(names, (tuple, list)) or len(names) < 2 or len(set(names)) != len(names):
        raise FreeCADRunnerError("invalid_loft", "loft requires distinct ordered sections")
    if document.getObject(name) is not None:
        raise FreeCADRunnerError("object_name_conflict", f"object already exists: {name}")
    profiles = [_object(document, p, "loft profile") for p in names]
    body = _body_for(document, profiles[0])
    for profile in profiles:
        _require_fully_constrained(profile)
        if _body_for(document, profile) != body:
            raise FreeCADRunnerError("invalid_loft", "all sections must belong to one Body")
    feature = document.addObject("PartDesign::SubtractiveLoft" if args.get("subtractive", False) else "PartDesign::AdditiveLoft", name)
    body.addObject(feature)
    feature.Profile = (profiles[0], [""])
    feature.Sections = [(p, [""]) for p in profiles[1:]]
    feature.Ruled = bool(args.get("ruled", False))
    return {"object": feature.Name, "type_id": feature.TypeId}


def _feature_sweep(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _name(args["name"], "sweep name")
    if document.getObject(name) is not None:
        raise FreeCADRunnerError("object_name_conflict", f"object already exists: {name}")
    profile = _object(document, args["profile"], "sweep profile")
    path = _object(document, args["path"], "sweep path")
    if profile == path:
        raise FreeCADRunnerError("invalid_sweep", "profile and path must be distinct")
    body = _body_for(document, profile)
    for sketch in (profile, path):
        _require_fully_constrained(sketch)
        if _body_for(document, sketch) != body:
            raise FreeCADRunnerError("invalid_sweep", "profile and path must belong to one Body")
    feature = document.addObject("PartDesign::SubtractivePipe" if args.get("subtractive", False) else "PartDesign::AdditivePipe", name)
    body.addObject(feature)
    feature.Profile = (profile, [""])
    feature.Spine = (path, [""])
    return {"object": feature.Name, "type_id": feature.TypeId}


def _origin_axis(body: Any, axis: str) -> Any:
    if axis not in {"x", "y", "z"}:
        raise FreeCADRunnerError("invalid_axis", "axis must be a Body origin axis")
    role = axis.upper() + "_Axis"
    matches = [o for o in body.Origin.OriginFeatures if getattr(o, "Role", None) == role]
    if len(matches) != 1:
        raise FreeCADRunnerError("invalid_axis", "Body axis could not be resolved uniquely")
    return matches[0]


def _feature_revolve(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _name(args["name"], "revolve name")
    if document.getObject(name) is not None:
        raise FreeCADRunnerError("object_name_conflict", f"object already exists: {name}")
    profile = _object(document, args["profile"], "revolve profile")
    _require_fully_constrained(profile)
    body = _body_for(document, profile)
    axis = _origin_axis(body, args["axis"])
    angle = _number(args["angle_deg"], "angle_deg", positive=True, maximum=360)
    feature = document.addObject("PartDesign::Groove" if args.get("subtractive", False) else "PartDesign::Revolution", name)
    body.addObject(feature)
    feature.Profile = (profile, [""])
    feature.ReferenceAxis = (axis, [""])
    feature.Angle = angle
    feature.Reversed = bool(args.get("reversed", False))
    return {"object": feature.Name, "type_id": feature.TypeId}


def _feature_pattern(document: Any, args: dict[str, Any], *, polar: bool) -> dict[str, Any]:
    name = _name(args["name"], "pattern name")
    if document.getObject(name) is not None:
        raise FreeCADRunnerError("object_name_conflict", f"object already exists: {name}")
    names = args["originals"]
    if not isinstance(names, (list, tuple)) or not names or len(names) != len(set(names)):
        raise FreeCADRunnerError("invalid_pattern", "pattern originals must be nonempty and unique")
    originals = [_object(document, n, "pattern original") for n in names]
    body = _body_for(document, originals[0])
    for original in originals:
        if _body_for(document, original) != body or not original.isDerivedFrom("PartDesign::Feature"):
            raise FreeCADRunnerError("invalid_pattern", "pattern originals must be features of one Body")
    count = args["occurrences"]
    if not isinstance(count, int) or isinstance(count, bool) or count < 2:
        raise FreeCADRunnerError("invalid_pattern", "occurrences must be an integer of at least two")
    axis = _origin_axis(body, args["axis"])
    size = _number(args["angle_deg"] if polar else args["length_mm"], "extent", positive=True,
                   maximum=360 if polar else 1_000_000)
    feature = document.addObject("PartDesign::PolarPattern" if polar else "PartDesign::LinearPattern", name)
    body.addObject(feature)
    feature.Originals = originals
    feature.TransformMode = "Features"
    feature.Mode = "Extent"
    feature.Occurrences = count
    feature.Reversed = bool(args.get("reversed", False))
    if polar:
        feature.Axis = (axis, [""])
        feature.Angle = size
    else:
        feature.Direction = (axis, [""])
        feature.Length = size
    # A transformed feature with no Originals is initially a MultiTransform
    # child in FreeCAD. Body.addObject cannot select it as Tip at that point.
    # Select the configured feature explicitly so exports contain the pattern.
    body.Tip = feature
    return {"object": feature.Name, "type_id": feature.TypeId}


def _feature_polar_pattern(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    return _feature_pattern(document, args, polar=True)


def _feature_linear_pattern(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    return _feature_pattern(document, args, polar=False)


def _feature_pocket(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _name(args["name"], "pocket name")
    if document.getObject(name) is not None:
        raise FreeCADRunnerError("object_name_conflict", f"object already exists: {name}")
    profile = _object(document, args["profile"], "profile")
    _require_fully_constrained(profile)
    body = _body_for(document, profile)
    feature = document.addObject("PartDesign::Pocket", name)
    body.addObject(feature)
    feature.Profile = profile
    through_all = bool(args.get("through_all", False))
    feature.Type = 1 if through_all else 0
    if not through_all:
        feature.Length = _number(args.get("length_mm"), "length_mm", positive=True)
    feature.Reversed = bool(args.get("reversed", False))
    return {"object": feature.Name, "type_id": feature.TypeId}


def _feature_hole(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _name(args["name"], "hole name")
    if document.getObject(name) is not None:
        raise FreeCADRunnerError("object_name_conflict", f"object already exists: {name}")
    profile = _object(document, args["profile"], "profile")
    _require_fully_constrained(profile)
    body = _body_for(document, profile)
    feature = document.addObject("PartDesign::Hole", name)
    body.addObject(feature)
    feature.Profile = profile
    feature.Diameter = _number(args["diameter_mm"], "diameter_mm", positive=True)
    through_all = bool(args.get("through_all", False))
    feature.DepthType = 1 if through_all else 0
    if not through_all:
        feature.Depth = _number(args.get("depth_mm"), "depth_mm", positive=True)
    feature.Reversed = bool(args.get("reversed", False))
    feature.ThreadType = 0
    feature.HoleCutType = 0
    feature.DrillPoint = 0
    feature.Tapered = 0
    cut = args.get("cut")
    if cut is not None:
        if not isinstance(cut, dict) or cut.get("kind") not in {"counterbore", "countersink"}:
            raise FreeCADRunnerError("invalid_hole_cut", "hole entrance requires an explicit supported kind")
        kind = cut["kind"]
        dimension = "depth_mm" if kind == "counterbore" else "angle_deg"
        if set(cut) != {"kind", "diameter_mm", dimension}:
            raise FreeCADRunnerError("invalid_hole_cut", "hole entrance dimensions must be explicit")
        diameter = _number(cut["diameter_mm"], "cut.diameter_mm", positive=True)
        if diameter <= float(feature.Diameter.Value):
            raise FreeCADRunnerError("invalid_hole_cut", "entrance must be wider than shaft")
        value = _number(cut[dimension], dimension, positive=True)
        if kind == "countersink" and value >= 180:
            raise FreeCADRunnerError("invalid_hole_cut", "countersink angle must be less than 180 degrees")
        entrance_depth = value if kind == "counterbore" else (diameter-float(feature.Diameter.Value))/(2*math.tan(math.radians(value/2)))
        if not through_all and entrance_depth >= float(feature.Depth.Value):
            raise FreeCADRunnerError("invalid_hole_cut", "entrance must leave a positive shaft depth")
        feature.HoleCutType = "Counterbore" if kind == "counterbore" else "Countersink"
        feature.HoleCutCustomValues = True
        feature.HoleCutDiameter = diameter
        feature.HoleCutDepth = value if kind == "counterbore" else 0
        if kind == "countersink":
            feature.HoleCutCountersinkAngle = value
    return {"object": feature.Name, "type_id": feature.TypeId}


def _dressup_target(document: Any, args: dict[str, Any], label: str) -> tuple[Any, Any]:
    target = _object(document, args["target"], label)
    if getattr(target, "TypeId", "") != "PartDesign::Body":
        return target, _body_for(document, target)
    # A Body is a container whose Tip will change when the dress-up is added.
    # Linking the new feature to the Body itself would create a cyclic base.
    # Snapshot its actual Tip before insertion; never pick an object by name.
    if args.get("selector") is not None:
        raise FreeCADRunnerError(
            "topology_target_mismatch",
            "a Body topology selection must be mapped to its feature before a dress-up",
        )
    tip = getattr(target, "Tip", None)
    if tip is None or tip == target or tip not in target.Group or not hasattr(tip, "Shape"):
        raise FreeCADRunnerError("invalid_dressup_target", "Body has no valid feature Tip")
    return tip, target


def _feature_fillet(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _name(args["name"], "fillet name")
    if document.getObject(name) is not None:
        raise FreeCADRunnerError("object_name_conflict", f"object already exists: {name}")
    target, body = _dressup_target(document, args, "fillet target")
    selector = args.get("selector")
    use_all_edges = args.get("use_all_edges", True)
    if (
        not isinstance(use_all_edges, bool)
        or use_all_edges == (selector is not None)
    ):
        raise FreeCADRunnerError(
            "invalid_edge_selection_mode",
            "fillet requires exactly one of use_all_edges or selector",
        )
    if selector is not None:
        try:
            resolved = resolve_topology_selector(
                document,
                selector,
                expected_revision_id=str(args.get("_expected_revision_id") or ""),
            )
        except TopologyResolutionError as exc:
            raise FreeCADRunnerError("topology_resolution_failed", str(exc)) from exc
        if resolved["object_name"] != target.Name:
            raise FreeCADRunnerError(
                "topology_target_mismatch",
                "fillet selector resolved a different object",
            )
        subelements = [str(resolved["subelement_name"])]
    else:
        subelements = [f"Face{index + 1}" for index in range(len(target.Shape.Faces))]
    if not subelements:
        raise FreeCADRunnerError("fillet_target_empty", f"{target.Name} has no faces")
    feature = document.addObject("PartDesign::Fillet", name)
    feature.Base = (target, subelements)
    feature.Radius = _number(args["radius_mm"], "radius_mm", positive=True)
    body.addObject(feature)
    feature.UseAllEdges = use_all_edges
    return {"object": feature.Name, "type_id": feature.TypeId}


def _feature_chamfer(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _name(args["name"], "chamfer name")
    if document.getObject(name) is not None:
        raise FreeCADRunnerError("object_name_conflict", f"object already exists: {name}")
    target, body = _dressup_target(document, args, "chamfer target")
    selector = args.get("selector")
    use_all_edges = args.get("use_all_edges", True)
    edge_scope = args.get("edge_scope")
    if (
        not isinstance(use_all_edges, bool)
        or sum((use_all_edges, selector is not None, edge_scope is not None)) != 1
    ):
        raise FreeCADRunnerError(
            "invalid_edge_selection_mode",
            "chamfer requires exactly one of use_all_edges, selector or edge_scope",
        )
    protected_faces = []
    if edge_scope is not None:
        try:
            subelements, protected_faces = resolve_edge_scope(target.Shape, edge_scope)
        except EdgeScopeError as exc:
            raise FreeCADRunnerError("edge_scope_unsupported", str(exc)) from exc
    elif selector is not None:
        try:
            resolved = resolve_topology_selector(
                document,
                selector,
                expected_revision_id=str(args.get("_expected_revision_id") or ""),
            )
        except TopologyResolutionError as exc:
            raise FreeCADRunnerError("topology_resolution_failed", str(exc)) from exc
        if resolved["object_name"] != target.Name:
            raise FreeCADRunnerError(
                "topology_target_mismatch",
                "chamfer selector resolved a different object",
            )
        subelements = [str(resolved["subelement_name"])]
    else:
        subelements = [f"Face{index + 1}" for index in range(len(target.Shape.Faces))]
    if not subelements:
        raise FreeCADRunnerError("chamfer_target_empty", f"{target.Name} has no faces")
    feature = document.addObject("PartDesign::Chamfer", name)
    feature.Base = (target, subelements)
    feature.Size = _number(args["size_mm"], "size_mm", positive=True)
    body.addObject(feature)
    feature.UseAllEdges = use_all_edges
    if protected_faces:
        document.recompute()
        if feature.Shape.isNull() or not feature.Shape.isValid():
            raise FreeCADRunnerError("chamfer_invalid_shape", "chamfer did not produce a valid solid")
        try:
            verify_protected_faces(protected_faces, feature.Shape)
        except EdgeScopeError as exc:
            raise FreeCADRunnerError("edge_scope_violation", str(exc)) from exc
    return {"object": feature.Name, "type_id": feature.TypeId}


def _bind_through_profile(feature: Any, *, changing_pad: Any = None) -> bool:
    """Retain the top-plane dependency of a planar through cut.

    Old CAD Agent documents used a numeric offset equal to the initial Pad
    length. Only migrate that unambiguous layout; preserve user expressions,
    face attachments, reversed pads and other coordinate systems unchanged.
    The expression is built from an allowlisted object name, never user code.
    """
    type_id = getattr(feature, "TypeId", "")
    mode = getattr(feature, "DepthType" if type_id == "PartDesign::Hole" else "Type", "")
    if type_id not in {"PartDesign::Hole", "PartDesign::Pocket"} or str(mode).replace(" ", "").lower() not in {"throughall", "1"}:
        return False
    pad = getattr(feature, "BaseFeature", None)
    seen = set()
    while pad is not None and pad.TypeId != "PartDesign::Pad":
        if pad.Name in seen:
            return False
        seen.add(pad.Name)
        pad = getattr(pad, "BaseFeature", None)
    if pad is None or (changing_pad is not None and pad != changing_pad) or pad.Reversed:
        return False
    profile = feature.Profile
    if isinstance(profile, (tuple, list)):
        profile = profile[0]
    support = getattr(profile, "AttachmentSupport", None)
    pad_profile = pad.Profile
    if isinstance(pad_profile, (tuple, list)):
        pad_profile = pad_profile[0]
    if (getattr(profile, "CADAgentPlacementSchema", None) == "body-frame.v1"
            and getattr(pad_profile, "CADAgentPlacementSchema", None) == "body-frame.v1"):
        if (getattr(pad, "Midplane", False) or getattr(feature, "Reversed", False)
                or str(getattr(pad, "Type", "Length")) not in {"Length", "0"}):
            return False
        normal = pad_profile.Placement.Rotation.multVec(App.Vector(0, 0, 1))
        cut_normal = profile.Placement.Rotation.multVec(App.Vector(0, 0, 1))
        displacement = profile.Placement.Base - pad_profile.Placement.Base
        length = float(pad.Length.Value)
        if ((normal-cut_normal).Length > 1e-7
                or not math.isclose(displacement.dot(normal), length, abs_tol=1e-7)):
            return False
        paths = ["Placement.Base."+axis for axis in "xyz"]
        if any(str(prop).startswith("Placement") for prop, _ in profile.ExpressionEngine):
            return False
        # Retain tangential offsets; only the measured pad-end dependency moves.
        tangent = displacement - normal*length
        for axis, path in zip("xyz", paths):
            component = getattr(normal, axis)
            if abs(component) > 1e-12:
                profile.setExpression(path, f"{_name(pad_profile.Name, 'pad profile')}.Placement.Base.{axis}"
                    f" + ({getattr(tangent, axis):.17g} mm) + ({component:.17g}) * {_name(pad.Name, 'pad')}.Length")
        return True
    if not support or support != getattr(pad_profile, "AttachmentSupport", None):
        return False
    if profile.MapReversed or pad_profile.MapReversed:
        return False
    normal = profile.Placement.Rotation.multVec(App.Vector(0, 0, 1))
    if (normal - App.Vector(0, 0, 1)).Length > 1e-7:
        return False
    length = float(pad.Length.Value)
    if not (
        math.isclose(pad.Shape.BoundBox.ZLength, length, abs_tol=1e-7)
        and math.isclose(pad.Shape.BoundBox.ZMin, 0.0, abs_tol=1e-7)
        and math.isclose(profile.AttachmentOffset.Base.z, length, abs_tol=1e-7)
        and math.isclose(profile.Placement.Base.z, pad.Shape.BoundBox.ZMax, abs_tol=1e-7)
    ):
        return False
    path = "AttachmentOffset.Base.z"
    if any(str(prop) == path for prop, _expression in profile.ExpressionEngine):
        return False
    profile.setExpression(path, f"{_name(pad.Name, 'pad name')}.Length")
    return True


def _property_set(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    obj = _object(document, args["object"], "property object")
    property_name = _name(args["property"], "property name")
    if property_name not in getattr(obj, "PropertiesList", ()):
        raise FreeCADRunnerError(
            "property_not_found", f"{obj.Name} has no property {property_name}"
        )
    property_type = str(obj.getTypeIdOfProperty(property_name))
    allowed_types = {
        "App::PropertyAngle",
        "App::PropertyBool",
        "App::PropertyDistance",
        "App::PropertyQuantityConstraint",
        "App::PropertyEnumeration",
        "App::PropertyFloat",
        "App::PropertyInteger",
        "App::PropertyIntegerConstraint",
        "App::PropertyLength",
        "App::PropertyString",
    }
    if property_type not in allowed_types:
        raise FreeCADRunnerError(
            "property_type_forbidden",
            f"property type is not allowlisted: {property_type}",
        )
    expected_property_type = args.get("expected_property_type")
    if expected_property_type is not None and property_type != expected_property_type:
        raise FreeCADRunnerError(
            "parameter_property_type_changed",
            "live FreeCAD property type differs from the verified state",
            details={
                "object": obj.Name,
                "property": property_name,
                "expected_property_type": str(expected_property_type),
                "actual_property_type": property_type,
            },
        )
    value = args["value"]
    if expected_property_type is not None and (
        isinstance(value, bool) or not isinstance(value, (int, float))
    ):
        raise FreeCADRunnerError(
            "parameter_value_type_invalid",
            "structured parameter value must be numeric",
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise FreeCADRunnerError(
            "parameter_value_type_invalid"
            if expected_property_type is not None
            else "invalid_property_value",
            "property value must be finite",
        )
    unit = args.get("unit")
    try:
        if obj.TypeId == "PartDesign::Pad" and property_name == "Length":
            for feature in document.Objects:
                _bind_through_profile(feature, changing_pad=obj)
        if expected_property_type in {
            "App::PropertyLength",
            "App::PropertyDistance",
            "App::PropertyQuantityConstraint",
        }:
            if unit != "mm":
                raise FreeCADRunnerError(
                    "parameter_value_type_invalid",
                    "length and distance parameters require millimetres",
                )
            setattr(obj, property_name, f"{float(value):.17g} mm")
        elif expected_property_type == "App::PropertyAngle":
            if unit != "deg":
                raise FreeCADRunnerError(
                    "parameter_value_type_invalid",
                    "angle parameters require degrees",
                )
            setattr(obj, property_name, f"{float(value):.17g} deg")
        elif expected_property_type in {"App::PropertyInteger", "App::PropertyIntegerConstraint"}:
            numeric = float(value)
            if not numeric.is_integer() or not -(2**31) <= numeric < 2**31:
                raise FreeCADRunnerError(
                    "parameter_value_type_invalid",
                    "integer parameter value is not representable",
                )
            setattr(obj, property_name, int(numeric))
        elif expected_property_type == "App::PropertyFloat":
            if unit is not None:
                raise FreeCADRunnerError(
                    "parameter_value_type_invalid",
                    "float parameters cannot declare a unit",
                )
            setattr(obj, property_name, float(value))
        else:
            setattr(obj, property_name, value)
    except FreeCADRunnerError:
        raise
    except Exception as exc:
        raise FreeCADRunnerError(
            "parameter_value_out_of_range",
            "FreeCAD rejected the structured parameter value",
            details={"object": obj.Name, "property": property_name},
        ) from exc
    if expected_property_type is not None:
        actual = getattr(obj, property_name)
        actual = float(getattr(actual, "Value", actual))
        if not math.isclose(actual, float(value), rel_tol=1e-12, abs_tol=1e-12):
            raise FreeCADRunnerError("parameter_value_out_of_range",
                "FreeCAD changed the requested parameter value; the edit was rejected",
                details={"object":obj.Name,"property":property_name,"requested":value,"actual":actual})
    return {"object": obj.Name, "property": property_name, "property_type": property_type}


def _document_export(_document: Any, args: dict[str, Any]) -> dict[str, Any]:
    formats = args.get("formats")
    if not isinstance(formats, list) or not formats or len(formats) != len(set(formats)):
        raise FreeCADRunnerError("invalid_export", "export formats must be a unique non-empty list")
    allowed = {"fcstd", "step", "stl", "dxf"}
    if any(not isinstance(item, str) or item not in allowed for item in formats):
        raise FreeCADRunnerError("invalid_export", "unsupported export format")
    if "fcstd" not in formats:
        raise FreeCADRunnerError("invalid_export", "fcstd output is required")
    basename = _name(args.get("basename", "model"), "export basename")
    if args.get('objects') is not None:
        names=args['objects']
        if not isinstance(names,list) or not names or len(set(names))!=len(names):
            raise FreeCADRunnerError('invalid_export','export objects must be a nonempty unique list')
        body_children={o.Name for body in _document.Objects if body.TypeId=='PartDesign::Body' for o in body.Group}
        for name in names:
            obj=_object(_document,name,'export object')
            if obj.Name in body_children and set(formats) != {'fcstd'}:
                raise FreeCADRunnerError('invalid_export','select the final Body, not an intermediate feature')
            shape=getattr(obj,'Shape',None)
            if set(formats) != {'fcstd'} and (shape is None or shape.isNull() or not shape.Solids):
                raise FreeCADRunnerError('invalid_export','selected export object has no final solid')
        ledger=_ledger(_document)
        if 'ExportObjects' not in ledger.PropertiesList:
            ledger.addProperty('App::PropertyStringList','ExportObjects','CAD Agent')
        ledger.ExportObjects=names
    return {"formats": formats, "basename": basename}


def _instance_placement(args):
    translation = args['translation_mm']
    axis = args.get('rotation_axis', [0, 0, 1])
    if not isinstance(translation, (list, tuple)) or len(translation) != 3 or not isinstance(axis, (list, tuple)) or len(axis) != 3:
        raise FreeCADRunnerError('invalid_placement', 'translation and rotation axis require three coordinates')
    vector = App.Vector(*[_number(v, 'rotation axis') for v in axis])
    if vector.Length < 1e-6:
        raise FreeCADRunnerError('invalid_placement', 'rotation axis cannot be zero')
    return App.Placement(App.Vector(*[_number(v, 'translation') for v in translation]),
        App.Rotation(vector, _number(args.get('rotation_deg', 0), 'rotation', minimum=-360, maximum=360)))


def _assembly_instance(document, args):
    name = _name(args['object'], 'instance name')
    source = _object(document, args['source'], 'source component')
    if document.getObject(name) is not None:
        raise FreeCADRunnerError('object_already_exists', 'instance name already exists')
    if source.TypeId not in {'PartDesign::Body', 'Part::Feature', 'PartDesign::Feature'} or source.Shape.isNull() or not source.Shape.Solids:
        raise FreeCADRunnerError('invalid_instance_source', 'instance source must be a solid component in this document')
    placement = _instance_placement(args)
    link = document.addObject('App::Link', name)
    link.setLink(source)
    link.Placement = placement
    return {'object': name}


def _assembly_place(document, args):
    link = _object(document, args['object'], 'instance')
    if link.TypeId != 'App::Link' or getattr(link, 'ElementCount', 0):
        raise FreeCADRunnerError('invalid_instance_target', 'placement target must be one explicit instance')
    link.Placement = _instance_placement(args)
    return {'object': link.Name}


DISPATCH = {
    "api.execute": execute_program,
    "document.inspect": lambda document, _args: {
        "object_count": len(document.Objects)
    },
    "sketch.create": _sketch_create,
    "sketch.add_geometry": _sketch_add_geometry,
    "sketch.add_profile": _sketch_add_profile,
    "sketch.add_constraint": _sketch_add_constraint,
    "sketch.set_constraint": _sketch_set_constraint,
    "feature.pad": _feature_pad,
    "feature.loft": _feature_loft,
    "feature.sweep": _feature_sweep,
    "feature.revolve": _feature_revolve,
    "feature.polar_pattern": _feature_polar_pattern,
    "feature.linear_pattern": _feature_linear_pattern,
    "feature.pocket": _feature_pocket,
    "feature.hole": _feature_hole,
    "feature.fillet": _feature_fillet,
    "feature.chamfer": _feature_chamfer,
    "property.set": _property_set,
    "document.export": _document_export,
    "assembly.instance": _assembly_instance,
    "assembly.place": _assembly_place,
}


def _validate_document(document: Any, *, op_id: str, action: str) -> dict[str, Any]:
    document.recompute()
    checked_shapes = 0
    sketch_states: list[dict[str, str | int | bool]] = []
    # Diagnose sketches before dependent Body/feature invalidity hides the cause.
    for obj in document.Objects:
        if getattr(obj, "TypeId", "") == "Sketcher::SketchObject":
            sketch_states.append(_validate_sketch(obj, op_id=op_id, action=action))
    for obj in document.Objects:
        if not bool(getattr(obj, "isValid", lambda: True)()):
            raise FreeCADRunnerError(
                "invalid_document_object",
                getattr(obj, "getStatusString", lambda: "object is invalid")(),
                op_id=op_id,
                action=action,
                details={"object": obj.Name},
            )
        if "Invalid" in {str(item) for item in getattr(obj, "State", ())}:
            raise FreeCADRunnerError(
                "invalid_document_object",
                f"object entered Invalid state: {obj.Name}",
                op_id=op_id,
                action=action,
            )
        if getattr(obj, "TypeId", "") == "Sketcher::SketchObject":
            # Sketch validity is owned by the solver contract above. Calling
            # OpenCascade Shape.check() on an intermediate sketch wire can
            # crash FreeCAD 1.1.3 inside BRep_Tool before the profile is
            # complete; solid features are still checked below.
            continue
        shape = getattr(obj, "Shape", None)
        if (
            shape is None
            or bool(getattr(shape, "isNull", lambda: True)())
            or len(getattr(shape, "Solids", ())) == 0
        ):
            continue
        try:
            errors = shape.check(True)
        except ValueError as exc:
            # FreeCAD raises ValueError for OpenCascade shape diagnostics such
            # as self-intersecting wires. Keep this at the shape-check boundary
            # so unrelated Python errors remain internal failures.
            raise FreeCADRunnerError(
                "shape_check_failed", f"Shape.check failed for {obj.Name}: {str(exc)[:1000]}",
                op_id=op_id, action=action,
                details={"object": obj.Name, "feature": getattr(getattr(obj, "Tip", None), "Name", obj.Name),
                         "exception_type": type(exc).__name__},
            ) from exc
        if errors:
            raise FreeCADRunnerError(
                "shape_check_failed",
                f"Shape.check failed for {obj.Name}: {str(errors)[:1000]}",
                op_id=op_id,
                action=action,
                details={"object": obj.Name, "feature": getattr(getattr(obj, "Tip", None), "Name", obj.Name)},
            )
        checked_shapes += 1
    return {
        "op_id": op_id,
        "action": action,
        "object_count": len(document.Objects),
        "checked_shapes": checked_shapes,
        "sketches": len(sketch_states),
    }


def _subtractive_baseline(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Capture the solid that a Pocket/Hole is required to remove material from."""

    profile = _object(document, args["profile"], "profile")
    body = _body_for(document, profile)
    document.recompute()
    for candidate in reversed(tuple(getattr(body, "Group", ()))):
        if candidate is profile:
            continue
        shape = getattr(candidate, "Shape", None)
        if (
            shape is None
            or bool(getattr(shape, "isNull", lambda: True)())
            or len(getattr(shape, "Solids", ())) == 0
        ):
            continue
        volume = float(getattr(shape, "Volume", 0.0))
        if math.isfinite(volume) and volume > 0:
            return {
                "object": str(candidate.Name),
                "volume": volume,
            }
    raise FreeCADRunnerError(
        "subtractive_feature_no_effect",
        "subtractive feature has no preceding solid in its PartDesign body",
        details={"profile": str(profile.Name)},
    )


def _validate_subtractive_effect(
    document: Any,
    result: dict[str, Any],
    baseline: dict[str, Any],
    *,
    op_id: str,
    action: str,
) -> None:
    """Fail closed when FreeCAD creates a subtractive feature without a cut."""

    result_name = result.get("object")
    feature = document.getObject(str(result_name)) if result_name is not None else None
    shape = getattr(feature, "Shape", None)
    solids = tuple(getattr(shape, "Solids", ())) if shape is not None else ()
    before_volume = float(baseline["volume"])
    after_volume = (
        float(getattr(shape, "Volume", 0.0))
        if shape is not None and not bool(getattr(shape, "isNull", lambda: True)())
        else 0.0
    )
    tolerance = max(1e-7, abs(before_volume) * 1e-9)
    if (
        not solids
        or not math.isfinite(after_volume)
        or after_volume <= 0
        or after_volume >= before_volume - tolerance
    ):
        details = {
            "base_object": str(baseline["object"]), "before_volume": before_volume,
            "result_object": str(result_name or ""), "after_volume": after_volume,
            "coordinate_space": "body", "diagnostic_version": "subtractive-evidence.v1",
        }
        profile = getattr(feature, "Profile", None)
        if isinstance(profile, (tuple, list)):
            profile = profile[0]
        if profile is not None:
            details["profile"] = profile.Name
            normal = profile.Placement.Rotation.multVec(App.Vector(0, 0, 1))
            for axis in ("x", "y", "z"):
                details[f"profile_origin_{axis}"] = float(getattr(profile.Placement.Base, axis))
                details[f"profile_normal_{axis}"] = float(getattr(normal, axis))
            details["placement_schema"] = str(getattr(profile, "CADAgentPlacementSchema", "legacy_attachment"))
        details["reversed"] = bool(getattr(feature, "Reversed", False))
        base = document.getObject(str(baseline["object"]))
        if base is not None:
            box = base.Shape.optimalBoundingBox(False, False)
            for axis in ("X", "Y", "Z"):
                for bound in ("Min", "Max"):
                    details[f"base_{axis.lower()}_{bound.lower()}"] = float(getattr(box, axis + bound))
        raise FreeCADRunnerError(
            "subtractive_feature_no_effect",
            f"{action} did not produce a smaller valid solid",
            op_id=op_id,
            action=action,
            details=details,
        )


def _open_document(task: dict[str, Any], plan: dict[str, Any]) -> Any:
    inputs = task["inputs"]
    base_name = inputs.get("base")
    if base_name is not None:
        if not isinstance(base_name, str) or Path(base_name).name != base_name:
            raise FreeCADRunnerError("invalid_base_artifact", "base input is not a safe filename")
        base = INPUT_ROOT / base_name
        if not base.is_file() or base.is_symlink() or base.suffix.lower() != ".fcstd":
            raise FreeCADRunnerError("invalid_base_artifact", "base FCStd input is missing")
        document = App.openDocument(str(base))
    else:
        document_name = _name(plan.get("document_name", "Model"), "document name")
        document = App.newDocument(document_name)
    document.UndoMode = 1
    return document


def _export_document(document: Any, args: dict[str, Any], *, measurement_formats=()) -> dict[str, str]:
    formats = list(args["formats"])
    basename = _name(args.get("basename", "model"), "export basename")
    files: dict[str, str] = {}
    fcstd = OUTPUT_ROOT / f"{basename}.FCStd"
    document.saveAs(str(fcstd))
    files["fcstd"] = str(fcstd)

    components = component_shapes(document)
    target_shape = Part.makeCompound([shape for _, shape in components]) if components else None
    derivative_formats = (set(formats) | set(measurement_formats)) - {"fcstd"}
    if derivative_formats and target_shape is None:
        raise FreeCADRunnerError("export_shape_missing", "document contains no solid shape to export")
    if "step" in formats:
        path = OUTPUT_ROOT / f"{basename}.step"
        target_shape.exportStep(str(path))
        files["step"] = str(path)
    elif "step" in measurement_formats:
        path = OUTPUT_ROOT / f"{basename}.verification.step"
        target_shape.exportStep(str(path))
        files["verification_step"] = str(path)
    if "stl" in formats:
        path = OUTPUT_ROOT / f"{basename}.stl"
        target_shape.exportStl(str(path))
        files["stl"] = str(path)
    if "dxf" in formats:
        path = OUTPUT_ROOT / f"{basename}.dxf"
        Import.export([obj for obj, _ in components], str(path))
        files["dxf"] = str(path)

    state_path = OUTPUT_ROOT / "state.json"
    state_path.write_text(
        json.dumps(
            project_saved_document(document, fcstd),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    files["state"] = str(state_path)
    for role, raw_path in files.items():
        path = Path(raw_path)
        if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
            raise FreeCADRunnerError("export_failed", f"{role} output was not produced")
    return files


def _validate_plan(task: dict[str, Any]) -> dict[str, Any]:
    if task.get("schema_version") != "mcad-capability-task.v1":
        raise FreeCADRunnerError("invalid_task", "unsupported capability task schema")
    if task.get("capability") != "freecad" or task.get("operation") != "execute":
        raise FreeCADRunnerError("invalid_task", "task is not a FreeCAD execute capability")
    params = task.get("params")
    inputs = task.get("inputs")
    if not isinstance(params, dict) or not isinstance(inputs, dict):
        raise FreeCADRunnerError("invalid_task", "task params and inputs must be objects")
    if set(params) - {"plan", "expected_revision_id", "measurement_formats"} or not isinstance(
        params.get("plan"), dict
    ):
        raise FreeCADRunnerError(
            "invalid_task",
            "FreeCAD task requires a plan and optional expected revision",
        )
    if "expected_revision_id" in params and (
        not isinstance(params["expected_revision_id"], str)
        or not params["expected_revision_id"]
        or len(params["expected_revision_id"]) > 128
    ):
        raise FreeCADRunnerError("invalid_task", "expected revision is invalid")
    plan = params["plan"]
    if params.get('measurement_formats', []) not in ([], ['step']):
        raise FreeCADRunnerError('invalid_task', 'unsupported internal measurement formats')
    if params.get('measurement_formats') and plan.get('execution_mode', 'final') != 'final':
        raise FreeCADRunnerError('invalid_task', 'checkpoint cannot request final measurement artifacts')
    if plan.get("schema_version") != "freecad-operation-plan.v1":
        raise FreeCADRunnerError("invalid_plan", "unsupported FreeCAD operation plan schema")
    if plan.get('execution_mode', 'final') not in {'final', 'checkpoint'}:
        raise FreeCADRunnerError('invalid_plan', 'unknown execution mode')
    operations = plan.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 200:
        raise FreeCADRunnerError("invalid_plan", "plan must contain 1 to 200 operations")
    seen: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) != {"op_id", "action", "args"}:
            raise FreeCADRunnerError("invalid_plan", "operation shape is invalid")
        op_id = operation["op_id"]
        if not isinstance(op_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,119}", op_id) is None:
            raise FreeCADRunnerError("invalid_plan", "operation ID is invalid")
        if op_id in seen:
            raise FreeCADRunnerError("invalid_plan", f"duplicate operation ID: {op_id}")
        seen.add(op_id)
        action = operation["action"]
        if action not in ACTION_KEYS:
            raise FreeCADRunnerError("invalid_plan", f"unsupported FreeCAD action: {action}")
        _keys(action, operation["args"])
    exports = [operation for operation in operations if operation["action"] == "document.export"]
    if len(exports) != 1 or operations[-1]["action"] != "document.export":
        raise FreeCADRunnerError("invalid_plan", "plan requires one final document.export")
    if any(op['action']=='api.execute' for op in operations) and [op['action'] for op in operations] != ['api.execute','document.export']:
        raise FreeCADRunnerError('invalid_plan','API plans require api.execute followed by document.export')
    if any(op['action']=='api.execute' for op in operations) and not exports[0]['args'].get('objects'):
        raise FreeCADRunnerError('invalid_plan','API plans must explicitly select final export objects')
    if set(inputs) - {"base"}:
        raise FreeCADRunnerError("invalid_task", "FreeCAD task accepts only the base input role")
    return plan


def run_task(task: dict[str, Any]) -> dict[str, Any]:
    plan = _validate_plan(task)
    document = _open_document(task, plan)
    ledger = _ledger(document)
    operation_results: list[dict[str, str | int | float | bool | None]] = []
    validations: list[dict[str, str | int | float | bool | None]] = []
    try:
        for operation in plan["operations"]:
            op_id = str(operation["op_id"])
            action = str(operation["action"])
            digest = _operation_digest(operation)
            existing = _ledger_records(ledger).get(op_id)
            if existing is not None:
                if existing != digest:
                    raise FreeCADRunnerError(
                        "operation_replay_conflict",
                        f"operation {op_id} was replayed with a different payload",
                        op_id=op_id,
                        action=action,
                    )
                operation_results.append(
                    {"op_id": op_id, "action": action, "status": "replayed"}
                )
                continue

            if action == 'api.execute':
                records = list(ledger.OperationRecords)
                try:
                    document = execute_program(document, _keys(action, operation['args']))
                    validation = _validate_document(document, op_id=op_id, action=action)
                    ledger = _ledger(document)
                    ledger.OperationRecords = [*records, f'{op_id}:{digest}']
                except Exception as exc:
                    if isinstance(exc,FreeCADRunnerError):
                        raise
                    raise FreeCADRunnerError(getattr(exc,'code','api_execution_failed'),str(exc),op_id=op_id,action=action) from exc
                operation_results.append({'op_id':op_id,'action':action,'status':'succeeded'})
                validations.append(validation)
                continue

            before_objects = {obj.Name for obj in document.Objects}
            document.openTransaction(f"CAD Agent {op_id}")
            try:
                operation_args = _keys(action, operation["args"])
                subtractive_baseline = (
                    _subtractive_baseline(document, {**operation_args,
                        "profile": operation_args["profiles"][0]} if action == "feature.loft" else operation_args)
                    if action in {"feature.hole", "feature.pocket"} or (action in {"feature.loft", "feature.sweep", "feature.revolve"} and operation_args.get("subtractive"))
                    else None
                )
                if action in {"feature.fillet", "feature.chamfer"} and operation_args.get(
                    "selector"
                ) is not None:
                    operation_args = {
                        **operation_args,
                        "_expected_revision_id": task["params"].get(
                            "expected_revision_id"
                        ),
                    }
                result = DISPATCH[action](document, operation_args)
                validation = _validate_document(document, op_id=op_id, action=action)
                if subtractive_baseline is not None:
                    _validate_subtractive_effect(
                        document,
                        result,
                        subtractive_baseline,
                        op_id=op_id,
                        action=action,
                    )
                    if _bind_through_profile(document.getObject(result["object"])):
                        validation = _validate_document(document, op_id=op_id, action=action)
                ledger.OperationRecords = [
                    *list(getattr(ledger, "OperationRecords", ())),
                    f"{op_id}:{digest}",
                ]
                document.commitTransaction()
            except Exception as exc:
                document.abortTransaction()
                leaked = sorted(
                    obj.Name for obj in document.Objects if obj.Name not in before_objects
                )
                if leaked:
                    raise FreeCADRunnerError(
                        "transaction_rollback_failed",
                        f"transaction left objects after abort: {', '.join(leaked)}",
                        op_id=op_id,
                        action=action,
                    ) from exc
                if isinstance(exc, FreeCADRunnerError):
                    if exc.op_id is None:
                        exc.op_id = op_id
                    if exc.action is None:
                        exc.action = action
                    raise
                raise FreeCADRunnerError(
                    "operation_failed",
                    str(exc) or type(exc).__name__,
                    op_id=op_id,
                    action=action,
                    details={"exception_type": type(exc).__name__},
                ) from exc
            operation_results.append(
                {
                    "op_id": op_id,
                    "action": action,
                    "status": "succeeded",
                    "object": result.get("object"),
                }
            )
            validations.append(validation)

        export_args = plan["operations"][-1]["args"]
        files = _export_document(document, export_args,
            measurement_formats=task['params'].get('measurement_formats', ()))
        return {
            "schema_version": "freecad-operation-result.v1",
            "status": "succeeded",
            "operations": operation_results,
            "files": files,
            "validations": validations,
            "runtime": {
                "freecad": ".".join(App.Version()[:3]),
                "python": sys.version.split()[0],
            },
        }
    finally:
        App.closeDocument(document.Name)


def main() -> int:
    try:
        task = json.loads((INPUT_ROOT / "task.json").read_text(encoding="utf-8"))
        if not isinstance(task, dict):
            raise FreeCADRunnerError("invalid_task", "capability task must be an object")
        if task.get('operation') == 'bom':
            result = run_bom(task)
        elif task.get('operation') == 'scene':
            result = run_scene(task)
        elif task.get('operation') == 'engineering':
            result = run_engineering(task,INPUT_ROOT,OUTPUT_ROOT)
        else:
            result = run_task(task)
        publish_result(result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        if isinstance(exc, EngineeringError):
            error = {'code':exc.code,'message':str(exc),'op_id':None,'action':'engineering','details':{}}
        elif isinstance(exc, BOMError):
            error = {
                "code": exc.code,
                "message": str(exc),
                "op_id": None,
                "action": "bom",
                "details": exc.details,
            }
        elif isinstance(exc, FreeCADRunnerError):
            error = {
                "code": exc.code,
                "message": str(exc),
                "op_id": exc.op_id,
                "action": exc.action,
                "details": exc.details,
            }
        else:
            error = {
                "code": "freecad_internal_error",
                "message": str(exc) or type(exc).__name__,
                "op_id": None,
                "action": None,
                "details": {"exception_type": type(exc).__name__},
            }
        result = {
            "schema_version": "freecad-operation-result.v1",
            "status": "failed",
            "operations": [],
            "files": {},
            "validations": [],
            "error": error,
            "traceback": traceback.format_exc(limit=20),
        }
        publish_result(result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    if exit_code != 0:
        raise RuntimeError("FreeCAD capability task failed")
