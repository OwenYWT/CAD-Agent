"""Pure projection of a live FreeCAD document into bounded JSON state."""

from __future__ import annotations

import math
from typing import Any


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
}
_FEATURE_PARAMETER_PROPERTIES = {
    "PartDesign::Pad": frozenset({"Length"}),
    "PartDesign::Pocket": frozenset({"Length"}),
    "PartDesign::Hole": frozenset({"Diameter", "Depth"}),
    "PartDesign::Fillet": frozenset({"Radius"}),
    "PartDesign::Chamfer": frozenset({"Size"}),
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


def _shape_state(shape: Any) -> dict[str, str | int | float] | None:
    if shape is None or bool(getattr(shape, "isNull", lambda: True)()):
        return None
    return {
        "type": str(getattr(shape, "ShapeType", "Unknown")),
        "faces": len(getattr(shape, "Faces", ())),
        "edges": len(getattr(shape, "Edges", ())),
        "solids": len(getattr(shape, "Solids", ())),
        "area": float(getattr(shape, "Area", 0.0)),
        "volume": float(getattr(shape, "Volume", 0.0)),
    }


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
    if property_type == "App::PropertyInteger":
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


def _constraint_status(solve_status: int, fully_constrained: bool) -> str:
    if solve_status < 0 and solve_status not in {-2, -3}:
        return "invalid"
    if solve_status == -3:
        return "conflicting"
    if solve_status == -2:
        return "redundant"
    if not fully_constrained:
        return "under_constrained"
    return "fully_constrained"


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
    shape = _shape_state(getattr(obj, "Shape", None))
    if shape is not None:
        projected["shape"] = shape
    if str(getattr(obj, "TypeId", "")) == "Sketcher::SketchObject":
        solve_status = int(obj.solve())
        fully_constrained = bool(getattr(obj, "FullyConstrained", False))
        projected["sketch"] = {
            "fully_constrained": fully_constrained,
            "constraint_count": int(getattr(obj, "ConstraintCount", 0)),
            "geometry_count": int(getattr(obj, "GeometryCount", 0)),
            "solver_status": solve_status,
            "constraint_status": _constraint_status(
                solve_status,
                fully_constrained,
            ),
        }
    return projected


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
