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

from freecad_state_projector import project_document
from freecad_topology import TopologyResolutionError, resolve_topology_selector
from freecad_bom import BOMError, run_bom


INPUT_ROOT = Path("/sandbox/input")
OUTPUT_ROOT = Path("/sandbox/output")
LEDGER_NAME = "CADAgentLedger"
NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
ACTION_KEYS: dict[str, tuple[set[str], set[str]]] = {
    "document.inspect": (set(), set()),
    "sketch.create": (
        {"name", "body", "plane", "offset_mm", "reversed"},
        {"name"},
    ),
    "sketch.add_geometry": ({"sketch", "geometry"}, {"sketch", "geometry"}),
    "sketch.add_constraint": (
        {"sketch", "kind", "first", "second", "value_mm"},
        {"sketch", "kind", "first"},
    ),
    "feature.pad": ({"name", "profile", "length_mm", "reversed"}, {"name", "profile", "length_mm"}),
    "feature.pocket": (
        {"name", "profile", "length_mm", "through_all", "reversed"},
        {"name", "profile"},
    ),
    "feature.hole": (
        {"name", "profile", "diameter_mm", "depth_mm", "through_all", "reversed"},
        {"name", "profile", "diameter_mm"},
    ),
    "feature.fillet": (
        {"name", "target", "radius_mm", "use_all_edges", "selector"},
        {"name", "target", "radius_mm"},
    ),
    "feature.chamfer": (
        {"name", "target", "size_mm", "use_all_edges", "selector"},
        {"name", "target", "size_mm"},
    ),
    "property.set": (
        {"object", "property", "value", "expected_property_type", "unit"},
        {"object", "property", "value"},
    ),
    "document.export": ({"formats", "basename"}, {"formats"}),
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
        amount = _number(value, "value_mm", positive=True)
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


def _require_fully_constrained(profile: Any) -> None:
    solve_status = int(profile.solve())
    if solve_status != 0:
        code = {
            -2: "sketch_redundant_constraints",
            -3: "sketch_conflicting_constraints",
        }.get(solve_status, "sketch_solver_failed")
        raise FreeCADRunnerError(
            code,
            f"Sketch {profile.Name} solver returned {solve_status}",
            details={"solver_status": solve_status},
        )
    if not bool(getattr(profile, "FullyConstrained", False)):
        raise FreeCADRunnerError(
            "sketch_under_constrained",
            f"Sketch {profile.Name} must be fully constrained before modeling",
        )


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
    return {"object": feature.Name, "type_id": feature.TypeId}


def _feature_fillet(document: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _name(args["name"], "fillet name")
    if document.getObject(name) is not None:
        raise FreeCADRunnerError("object_name_conflict", f"object already exists: {name}")
    target = _object(document, args["target"], "fillet target")
    body = _body_for(document, target)
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
    target = _object(document, args["target"], "chamfer target")
    body = _body_for(document, target)
    selector = args.get("selector")
    use_all_edges = args.get("use_all_edges", True)
    if (
        not isinstance(use_all_edges, bool)
        or use_all_edges == (selector is not None)
    ):
        raise FreeCADRunnerError(
            "invalid_edge_selection_mode",
            "chamfer requires exactly one of use_all_edges or selector",
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
        elif expected_property_type == "App::PropertyInteger":
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
    return {"formats": formats, "basename": basename}


DISPATCH = {
    "document.inspect": lambda document, _args: {
        "object_count": len(document.Objects)
    },
    "sketch.create": _sketch_create,
    "sketch.add_geometry": _sketch_add_geometry,
    "sketch.add_constraint": _sketch_add_constraint,
    "feature.pad": _feature_pad,
    "feature.pocket": _feature_pocket,
    "feature.hole": _feature_hole,
    "feature.fillet": _feature_fillet,
    "feature.chamfer": _feature_chamfer,
    "property.set": _property_set,
    "document.export": _document_export,
}


def _validate_document(document: Any, *, op_id: str, action: str) -> dict[str, Any]:
    document.recompute()
    checked_shapes = 0
    sketch_states: list[dict[str, str | int | bool]] = []
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
            solve_status = int(obj.solve())
            sketch_states.append(
                {
                    "name": obj.Name,
                    "solver_status": solve_status,
                    "fully_constrained": bool(getattr(obj, "FullyConstrained", False)),
                }
            )
            if solve_status < 0:
                code = {
                    -2: "sketch_redundant_constraints",
                    -3: "sketch_conflicting_constraints",
                }.get(solve_status, "sketch_solver_failed")
                raise FreeCADRunnerError(
                    code,
                    f"Sketch {obj.Name} solver returned {solve_status}",
                    op_id=op_id,
                    action=action,
                    details={"object": obj.Name, "solver_status": solve_status},
                )
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
        errors = shape.check(True)
        if errors:
            raise FreeCADRunnerError(
                "shape_check_failed",
                f"Shape.check failed for {obj.Name}: {str(errors)[:1000]}",
                op_id=op_id,
                action=action,
                details={"object": obj.Name},
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
        raise FreeCADRunnerError(
            "subtractive_feature_no_effect",
            f"{action} did not produce a smaller valid solid",
            op_id=op_id,
            action=action,
            details={
                "base_object": str(baseline["object"]),
                "before_volume": before_volume,
                "result_object": str(result_name or ""),
                "after_volume": after_volume,
            },
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


def _export_document(document: Any, args: dict[str, Any]) -> dict[str, str]:
    formats = list(args["formats"])
    basename = _name(args.get("basename", "model"), "export basename")
    files: dict[str, str] = {}
    fcstd = OUTPUT_ROOT / f"{basename}.FCStd"
    document.saveAs(str(fcstd))
    files["fcstd"] = str(fcstd)

    target = next(
        (
            obj
            for obj in reversed(document.Objects)
            if getattr(obj, "Shape", None) is not None
            and not bool(getattr(obj.Shape, "isNull", lambda: True)())
            and len(getattr(obj.Shape, "Solids", ())) > 0
        ),
        None,
    )
    derivative_formats = set(formats) - {"fcstd"}
    if derivative_formats and target is None:
        raise FreeCADRunnerError("export_shape_missing", "document contains no solid shape to export")
    if "step" in formats:
        path = OUTPUT_ROOT / f"{basename}.step"
        target.Shape.exportStep(str(path))
        files["step"] = str(path)
    if "stl" in formats:
        path = OUTPUT_ROOT / f"{basename}.stl"
        target.Shape.exportStl(str(path))
        files["stl"] = str(path)
    if "dxf" in formats:
        path = OUTPUT_ROOT / f"{basename}.dxf"
        Import.export([target], str(path))
        files["dxf"] = str(path)

    state_path = OUTPUT_ROOT / "state.json"
    state_path.write_text(
        json.dumps(
            project_document(document),
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
    if set(params) - {"plan", "expected_revision_id"} or not isinstance(
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
    if plan.get("schema_version") != "freecad-operation-plan.v1":
        raise FreeCADRunnerError("invalid_plan", "unsupported FreeCAD operation plan schema")
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

            before_objects = {obj.Name for obj in document.Objects}
            document.openTransaction(f"CAD Agent {op_id}")
            try:
                operation_args = _keys(action, operation["args"])
                subtractive_baseline = (
                    _subtractive_baseline(document, operation_args)
                    if action in {"feature.hole", "feature.pocket"}
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
        files = _export_document(document, export_args)
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
        result = (
            run_bom(task)
            if task.get("operation") == "bom"
            else run_task(task)
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        if isinstance(exc, BOMError):
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
        print(
            json.dumps(
                {
                    "schema_version": "freecad-operation-result.v1",
                    "status": "failed",
                    "operations": [],
                    "files": {},
                    "validations": [],
                    "error": error,
                    "traceback": traceback.format_exc(limit=20),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    if exit_code != 0:
        raise RuntimeError("FreeCAD capability task failed")
