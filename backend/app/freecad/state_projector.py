"""Pure projection of a live FreeCAD document into bounded JSON state."""

from __future__ import annotations

import math
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

try:
    from freecad_sketch_diagnostics import diagnose_sketch
except ModuleNotFoundError:
    from .sketch_diagnostics import diagnose_sketch

try:
    from freecad_reference_geometry import REFERENCE_KINDS, normalize_reference_object
except ModuleNotFoundError:
    from .reference_geometry import REFERENCE_KINDS, normalize_reference_object


_SKIP_PROPERTIES = {
    "Constraints",
    "ExpressionEngine",
    "Geometry",
    "GeometryCount",
    "Label2",
    "Proxy",
    "Shape",
    "ViewObject",
}
_EDITABLE_PROPERTY_TYPES = {
    "App::PropertyLength": "mm",
    "App::PropertyDistance": "mm",
    "App::PropertyQuantityConstraint": "mm",
    "App::PropertyAngle": "deg",
    "App::PropertyFloat": None,
    "App::PropertyInteger": None,
    "App::PropertyIntegerConstraint": None,
}
_FEATURE_PARAMETER_PROPERTIES = {
    "PartDesign::Pad": frozenset({"Length"}),
    "PartDesign::Pocket": frozenset({"Length"}),
    "PartDesign::Hole": frozenset({"Diameter", "Depth", "HoleCutDiameter", "HoleCutDepth", "HoleCutCountersinkAngle"}),
    "PartDesign::Fillet": frozenset({"Radius"}),
    "PartDesign::Chamfer": frozenset({"Size"}),
    "PartDesign::Revolution": frozenset({"Angle"}),
    "PartDesign::Groove": frozenset({"Angle"}),
    "PartDesign::PolarPattern": frozenset({"Angle", "Occurrences"}),
    "PartDesign::LinearPattern": frozenset({"Length", "Occurrences"}),
}


def _bounded_text(value: Any, limit: int = 500) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _property_value(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    user_string = getattr(value, "UserString", None)
    if isinstance(user_string, str):
        return _bounded_text(user_string)
    name = getattr(value, "Name", None)
    if isinstance(name, str):
        return name
    return _bounded_text(value)


def _shape_state(shape: Any) -> dict[str, Any] | None:
    if shape is None or bool(getattr(shape, "isNull", lambda: True)()):
        return None
    result = {
        "type": str(getattr(shape, "ShapeType", "Unknown")),
        "faces": len(getattr(shape, "Faces", ())),
        "edges": len(getattr(shape, "Edges", ())),
        "solids": len(getattr(shape, "Solids", ())),
        "area": float(getattr(shape, "Area", 0.0)),
        "volume": float(getattr(shape, "Volume", 0.0)),
    }
    bounds = getattr(shape, "BoundBox", None)
    if bounds is not None:
        points = [[float(getattr(bounds, axis + end)) for axis in "XYZ"] for end in ("Min", "Max")]
        if all(math.isfinite(value) for point in points for value in point):
            result["bounds_mm"] = {"min": points[0], "max": points[1]}
    return result


def _editor_mode(obj: Any, name: str) -> int:
    getter = getattr(obj, "getEditorMode", None)
    if not callable(getter):
        return 1
    value = getter(name)
    if isinstance(value, (int, float)):
        return int(value)
    # FreeCAD 1.1 exposes editor flags as a list of names. An empty list is the
    # editable state; values such as ["ReadOnly"] must remain excluded.
    if isinstance(value, (list, tuple, set, frozenset)):
        return 0 if not value else 1
    return 1


def _has_expression(obj: Any, name: str) -> bool:
    getter = getattr(obj, "getExpression", None)
    if not callable(getter):
        return False
    expression = getter(name)
    if isinstance(expression, tuple):
        return bool(expression and expression[0])
    return bool(expression)


def _numeric_parameter_value(value: Any, property_type: str) -> int | float | None:
    if property_type in {"App::PropertyInteger", "App::PropertyIntegerConstraint"}:
        if isinstance(value, bool):
            return None
        numeric = int(value)
        return numeric if -(2**31) <= numeric < 2**31 else None
    unit = _EDITABLE_PROPERTY_TYPES[property_type]
    if unit is not None:
        converter = getattr(value, "getValueAs", None)
        if callable(converter):
            converted = converter(unit)
            numeric = float(getattr(converted, "Value", converted))
        else:
            numeric = float(getattr(value, "Value", value))
    else:
        numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _uses_finite_dimension(value: Any) -> bool:
    """Normalize FreeCAD enumeration values without assuming their ABI shape."""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) == 0
    normalized = "".join(character for character in str(value).lower() if character.isalnum())
    return normalized in {"0", "dimension", "length"}


def project_parameters(obj: Any) -> list[dict[str, Any]]:
    allowed = set(
        _FEATURE_PARAMETER_PROPERTIES.get(str(getattr(obj, "TypeId", "")), ())
    )
    if str(getattr(obj, "TypeId", "")) == "PartDesign::Hole":
        kind = str(getattr(obj, "HoleCutType", "None"))
        if kind not in {"Counterbore", "Countersink"} or not getattr(obj, "HoleCutCustomValues", False):
            allowed.difference_update({"HoleCutDiameter", "HoleCutDepth", "HoleCutCountersinkAngle"})
        elif kind == "Counterbore":
            allowed.discard("HoleCutCountersinkAngle")
        else:
            # Recessing the head is a different design change; this contract
            # controls mouth diameter and cone angle, preserving the entrance plane.
            allowed.discard("HoleCutDepth")
    # A through-all cut has no user-controlled finite depth even though
    # FreeCAD retains an internal numeric Depth/Length value on the feature.
    if str(getattr(obj, "TypeId", "")) == "PartDesign::Hole" and not (
        _uses_finite_dimension(getattr(obj, "DepthType", None))
    ):
        allowed.discard("Depth")
    if str(getattr(obj, "TypeId", "")) == "PartDesign::Pocket" and not (
        _uses_finite_dimension(getattr(obj, "Type", None))
    ):
        allowed.discard("Length")
    result: list[dict[str, Any]] = []
    for name in sorted(getattr(obj, "PropertiesList", ())):
        if name not in allowed:
            continue
        try:
            property_type = str(obj.getTypeIdOfProperty(name))
            if property_type not in _EDITABLE_PROPERTY_TYPES:
                continue
            if _editor_mode(obj, name) != 0 or _has_expression(obj, name):
                continue
            value = _numeric_parameter_value(getattr(obj, name), property_type)
            if value is None:
                continue
            group_getter = getattr(obj, "getGroupOfProperty", None)
            group = str(group_getter(name)) if callable(group_getter) else "Data"
            result.append({
                "id": f"{obj.Name}.{name}",
                "object_name": str(obj.Name),
                "property_name": name,
                "label": (
                    f"{_bounded_text(getattr(obj, 'Label', obj.Name), 120)}"
                    f" · {name}"
                ),
                "group": group or "Data",
                "property_type": property_type,
                "value": value,
                "unit": _EDITABLE_PROPERTY_TYPES[property_type],
                "editable": True,
                "minimum": None,
                "maximum": None,
                "step": None,
            })
        except Exception:
            continue
    return result


def _vector(value):
    return [float(value.x), float(value.y), float(value.z)]


def _inspection(obj):
    """Finite kernel facts for on-demand inspection, never permanent FaceN IDs."""
    result = {}
    if str(getattr(obj, "TypeId", "")) == "Sketcher::SketchObject":
        constraints = list(getattr(obj, "Constraints", ()))
        items = []
        for index,c in enumerate(constraints[:256]):
            item = {"index":index,"type":str(c.Type),
                "first":int(c.First),"first_position":int(c.FirstPos),
                "second":int(c.Second),"second_position":int(c.SecondPos),
                "value":float(c.Value),"name":str(getattr(c,"Name",""))}
            if str(c.Type) in {'DistanceX','DistanceY','Distance','Radius','Diameter','Angle'} and callable(getattr(obj,'getDriving',None)):
                item['driving'] = bool(obj.getDriving(index))
            items.append(item)
        result["constraints"] = {"total":len(constraints),"items":items}
        geometries = list(getattr(obj,"Geometry", ()))
        items = []
        for index,g in enumerate(geometries[:256]):
            item = {"index":index,"type":str(getattr(g,"TypeId",type(g).__name__))}
            for name,key in (("StartPoint","start"),("EndPoint","end"),("Center","center")):
                if hasattr(g,name):
                    item[key] = _vector(getattr(g,name))
            if hasattr(g,"Radius"):
                item["radius_mm"] = float(g.Radius)
            if callable(getattr(obj,'getConstruction',None)):
                item['construction'] = bool(obj.getConstruction(index))
            items.append(item)
        result["geometry"] = {"total":len(geometries),"items":items}
    shape = getattr(obj,"Shape",None)
    if shape is not None and not bool(getattr(shape,"isNull",lambda:True)()):
        faces = list(getattr(shape,"Faces", ()))
        edges = list(getattr(shape,"Edges", ()))
        items = []
        for face in faces[:64]:
            try:
                item = {"kind":"face","geometry":type(face.Surface).__name__,
                        "geometry_type":str(getattr(face.Surface,"TypeId","")),
                        "center":_vector(face.CenterOfMass),"area_mm2":float(face.Area),
                        "normal":_vector(face.normalAt(0,0))}
                items.append(item)
            except (AttributeError,RuntimeError,ValueError,TypeError) as exc:
                items.append({"kind":"face","status":"unavailable","error":type(exc).__name__})
        for edge in edges[:64]:
            try:
                item = {"kind":"edge","geometry":type(edge.Curve).__name__,
                        "geometry_type":str(getattr(edge.Curve,"TypeId","")),
                        "center":_vector(edge.CenterOfMass),"length_mm":float(edge.Length)}
                if hasattr(edge.Curve,"Radius"):
                    item["radius_mm"] = float(edge.Curve.Radius)
                if hasattr(edge.Curve,"Center") and hasattr(edge.Curve,"Axis"):
                    item["curve_center"] = _vector(edge.Curve.Center)
                    item["curve_axis"] = _vector(edge.Curve.Axis)
                items.append(item)
            except (AttributeError,RuntimeError,ValueError,TypeError) as exc:
                # OCC degenerate edges (e.g. sphere poles) have no curve and
                # FreeCAD raises TypeError. This is a missing measurement,
                # not an invalid solid or a reason to reject its export.
                items.append({"kind":"edge","status":"unavailable","error":type(exc).__name__})
        result["topology"] = {"total":len(faces)+len(edges), "total_faces":len(faces),
                              "total_edges":len(edges), "items":items}
    return result


def project_object(obj: Any) -> dict[str, Any]:
    properties: dict[str, str | int | float | bool | None] = {}
    for name in sorted(getattr(obj, "PropertiesList", ()))[:100]:
        if name in _SKIP_PROPERTIES or name.startswith("_Agent"):
            continue
        try:
            properties[name] = _property_value(getattr(obj, name))
        except Exception:
            properties[name] = "<unavailable>"

    projected: dict[str, Any] = {
        "name": str(obj.Name),
        "label": _bounded_text(getattr(obj, "Label", obj.Name), 240),
        "type_id": str(getattr(obj, "TypeId", "")),
        "state": [str(item) for item in getattr(obj, "State", ())],
        "is_valid": bool(getattr(obj, "isValid", lambda: True)()),
        "in": sorted(
            str(item.Name) for item in getattr(obj, "InList", ()) if hasattr(item, "Name")
        ),
        "out": sorted(
            str(item.Name) for item in getattr(obj, "OutList", ()) if hasattr(item, "Name")
        ),
        "properties": properties,
    }
    type_id = projected["type_id"]
    categories = {"PartDesign::Body": "body", "App::Part": "part", "App::DocumentObjectGroup": "group",
                  "App::Link": "instance", "Sketcher::SketchObject": "sketch",
                  "App::Origin": "datum", "App::Plane": "datum", "App::Line": "datum", "App::Point": "datum"}
    structure = {"status": "measured", "category": categories.get(type_id, "feature" if type_id.startswith("PartDesign::") else "other"),
                 "members": [], "body_tip": None}
    if type_id in {"PartDesign::Body", "App::Part", "App::DocumentObjectGroup"}:
        try:
            # Group preserves the kernel's container order. OutList is a
            # dependency graph and must never be used as a tree substitute.
            structure["members"] = [str(member.Name) for member in obj.Group]
            if type_id == "PartDesign::Body":
                structure["body_tip"] = str(obj.Tip.Name) if obj.Tip is not None else None
        except (AttributeError, RuntimeError, TypeError):
            structure = {"status": "unavailable", "category": structure["category"],
                         "reason": "native_container_members_unavailable"}
    projected["structure"] = structure
    is_reference = type_id in REFERENCE_KINDS
    native_shape = getattr(obj, "Shape", None)
    shape = None if is_reference else _shape_state(native_shape)
    if shape is not None:
        projected["shape"] = shape
    if native_shape is not None and not bool(getattr(native_shape, "isNull", lambda: True)()):
        exporter = getattr(native_shape, "exportBrepToString", None)
        if callable(exporter):
            projected["geometry_sha256"] = hashlib.sha256(exporter().encode("utf-8")).hexdigest()
    if str(getattr(obj, "TypeId", "")) == "Sketcher::SketchObject":
        projected["sketch"] = {
            **diagnose_sketch(obj),
            "constraint_count": int(getattr(obj, "ConstraintCount", 0)),
            "geometry_count": int(getattr(obj, "GeometryCount", 0)),
        }
    projected["inspection"] = {} if is_reference else _inspection(obj)
    if str(getattr(obj, 'TypeId', '')) == 'App::Link':
        placement = obj.Placement
        projected['instance'] = {'source':obj.getLinkedObject(True).Name,
            'translation_mm':list(placement.Base), 'rotation_axis':list(placement.Rotation.Axis),
            'rotation_deg':math.degrees(placement.Rotation.Angle)}
    global_placement = getattr(obj, "getGlobalPlacement", None)
    if callable(global_placement):
        projected["global_placement"] = list(global_placement().toMatrix().A)
    if projected.get("sketch") is not None:
        constraints = projected['inspection'].get('constraints')
        # Additional inspector metadata must not change the established
        # structural fingerprint for an otherwise identical saved sketch.
        fingerprint_constraints = {**constraints, 'items':[
            {k:v for k,v in c.items() if k != 'driving'} for c in constraints['items']
        ]} if constraints else constraints
        projected["sketch_constraints_sha256"] = hashlib.sha256(
            json.dumps(fingerprint_constraints,
                sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    return normalize_reference_object(projected)


def project_document(document: Any) -> dict[str, Any]:
    ordered = list(getattr(document, "TopologicalSortedObjects", ()))
    if not ordered:
        ordered = list(getattr(document, "Objects", ()))
    objects = [project_object(obj) for obj in ordered]
    parameters = [
        parameter
        for obj in ordered
        for parameter in project_parameters(obj)
    ]
    return {
        "schema_version": "freecad-state.v2",
        "document": str(document.Name),
        "object_count": len(objects),
        "root_objects": [
            str(obj.Name) for obj in getattr(document, "RootObjects", ())
        ],
        "objects": objects,
        "parameters": parameters,
    }


def project_saved_document(document: Any, path: str | Path) -> dict[str, Any]:
    """Project the actual FCStd checkpoint, including its persisted topology.

    OCC's in-memory BRep flags differ after FCStd persistence. Comparing an
    unsaved in-memory hash with a restored hash can reject independent edits.
    A separate filename forces FreeCAD to load the saved bytes instead of
    returning the already open document. Never recompute this readback.
    """
    import FreeCAD as App

    with tempfile.TemporaryDirectory(prefix="cad-checkpoint-") as directory:
        copy_path = Path(directory) / "checkpoint.FCStd"
        shutil.copyfile(path, copy_path)
        restored = App.openDocument(str(copy_path))
        try:
            state = project_document(restored)
            state["document"] = str(document.Name)
            for obj in state["objects"]:
                if obj.get("geometry_sha256"):
                    obj["geometry_fingerprint_kind"] = "fcstd-brep.v1"
            return state
        finally:
            App.closeDocument(restored.Name)
