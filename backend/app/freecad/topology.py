"""FreeCAD semantic topology resolver.

This module stays stdlib-only so the same implementation runs under Debian's
FreeCAD Python ABI inside the sandbox image.
"""

from __future__ import annotations

import math
import re
from typing import Any


_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
_AXES = {"x": 0, "y": 1, "z": 2}


class TopologyResolutionError(RuntimeError):
    pass


def _component(vector: Any, axis: str) -> float:
    return float((vector.x, vector.y, vector.z)[_AXES[axis]])


def _distance(first: Any, second: dict[str, Any]) -> float:
    return math.sqrt(
        (float(first.x) - float(second["x"])) ** 2
        + (float(first.y) - float(second["y"])) ** 2
        + (float(first.z) - float(second["z"])) ** 2
    )


def _validate_selector(selector: object) -> dict[str, Any]:
    if not isinstance(selector, dict):
        raise TopologyResolutionError("selector must be an object")
    allowed = {
        "schema_version",
        "backend",
        "revision_id",
        "object_name",
        "subelement_kind",
        "geometry",
        "axis",
        "extreme",
        "normal_sign",
        "radius_mm",
        "center",
        "tolerance_mm",
    }
    if set(selector) - allowed:
        raise TopologyResolutionError("selector contains unsupported fields")
    if selector.get("schema_version") != "topology-selector.v1":
        raise TopologyResolutionError("unsupported topology selector schema")
    if selector.get("backend") != "freecad":
        raise TopologyResolutionError("selector backend must be freecad")
    name = selector.get("object_name")
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise TopologyResolutionError("selector object_name is invalid")
    kind = selector.get("subelement_kind")
    geometry = selector.get("geometry")
    if (kind, geometry) not in {("face", "planar"), ("edge", "circular")}:
        raise TopologyResolutionError("selector kind and geometry are incompatible")
    if selector.get("axis") not in _AXES or selector.get("extreme") not in {
        "min",
        "max",
    }:
        raise TopologyResolutionError("selector axis/extreme is invalid")
    tolerance = selector.get("tolerance_mm", 1e-5)
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not math.isfinite(float(tolerance))
        or not 0 < float(tolerance) <= 10
    ):
        raise TopologyResolutionError("selector tolerance_mm is invalid")
    return selector


def _canonical_name(obj: Any, ephemeral_name: str) -> tuple[str, str]:
    resolver = getattr(obj, "resolveSubElement", None)
    if resolver is None:
        return ephemeral_name, "geometry"
    try:
        resolved_obj, new_name, _old_name = resolver(ephemeral_name, False, 0)
    except Exception:
        return ephemeral_name, "geometry"
    if resolved_obj is obj and isinstance(new_name, str) and new_name:
        return new_name, "geometry+resolveSubElement"
    return ephemeral_name, "geometry"


def _resolve_planar_face(obj: Any, selector: dict[str, Any]) -> dict[str, Any]:
    axis = str(selector["axis"])
    tolerance = float(selector.get("tolerance_mm", 1e-5))
    normal_sign = selector.get("normal_sign")
    candidates: list[tuple[float, float, int, Any]] = []
    for index, face in enumerate(getattr(obj.Shape, "Faces", ()), start=1):
        try:
            # A cylinder/cone can have an axis-aligned normal at one parameter.
            # Require the kernel's actual analytic surface type before using it.
            if getattr(getattr(face, "Surface", None), "TypeId", None) != "Part::GeomPlane":
                continue
            normal = face.normalAt(0, 0)
            alignment = _component(normal, axis)
            if abs(abs(alignment) - 1.0) > tolerance:
                continue
            if normal_sign is not None and alignment * int(normal_sign) <= 0:
                continue
            position = _component(face.CenterOfMass, axis)
            candidates.append((position, float(face.Area), index, face))
        except Exception:
            continue
    if not candidates:
        raise TopologyResolutionError("no planar face matches the selector")
    reverse = selector["extreme"] == "max"
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=reverse)
    best = candidates[0]
    tied = [item for item in candidates if abs(item[0] - best[0]) <= tolerance]
    if len(tied) > 1:
        raise TopologyResolutionError("planar face selector is ambiguous")
    ephemeral = f"Face{best[2]}"
    name, method = _canonical_name(obj, ephemeral)
    return {
        "object_name": str(obj.Name),
        "subelement_name": name,
        "subelement_kind": "face",
        "resolution_method": method,
        "signature": {
            "geometry": "planar",
            "axis": axis,
            "position_mm": best[0],
            "area_mm2": best[1],
        },
    }


def _resolve_circular_edge(obj: Any, selector: dict[str, Any]) -> dict[str, Any]:
    axis = str(selector["axis"])
    tolerance = float(selector.get("tolerance_mm", 1e-5))
    radius = selector.get("radius_mm")
    if (
        isinstance(radius, bool)
        or not isinstance(radius, (int, float))
        or not math.isfinite(float(radius))
        or float(radius) <= 0
    ):
        raise TopologyResolutionError("circular selector radius_mm is invalid")
    center = selector.get("center")
    if center is not None and (
        not isinstance(center, dict) or set(center) != {"x", "y", "z"}
    ):
        raise TopologyResolutionError("circular selector center is invalid")
    candidates: list[tuple[float, int, Any, float, Any]] = []
    for index, edge in enumerate(getattr(obj.Shape, "Edges", ()), start=1):
        curve = getattr(edge, "Curve", None)
        curve_radius = getattr(curve, "Radius", None)
        curve_center = getattr(curve, "Center", None)
        curve_axis = getattr(curve, "Axis", None)
        if curve_radius is None or curve_center is None or curve_axis is None:
            continue
        if abs(float(curve_radius) - float(radius)) > tolerance:
            continue
        if abs(abs(_component(curve_axis, axis)) - 1.0) > tolerance:
            continue
        if center is not None and _distance(curve_center, center) > tolerance:
            continue
        position = _component(curve_center, axis)
        candidates.append((position, index, edge, float(curve_radius), curve_center))
    if not candidates:
        raise TopologyResolutionError("no circular edge matches the selector")
    reverse = selector["extreme"] == "max"
    candidates.sort(key=lambda item: item[0], reverse=reverse)
    best = candidates[0]
    if len(candidates) > 1 and abs(candidates[1][0] - best[0]) <= tolerance:
        raise TopologyResolutionError("circular edge selector is ambiguous")
    ephemeral = f"Edge{best[1]}"
    name, method = _canonical_name(obj, ephemeral)
    return {
        "object_name": str(obj.Name),
        "subelement_name": name,
        "subelement_kind": "edge",
        "resolution_method": method,
        "signature": {
            "geometry": "circular",
            "axis": axis,
            "position_mm": best[0],
            "radius_mm": best[3],
            "center_mm": {
                "x": float(best[4].x),
                "y": float(best[4].y),
                "z": float(best[4].z),
            },
        },
    }


def resolve_topology_selector(
    document: Any,
    selector: object,
    *,
    expected_revision_id: str,
) -> dict[str, Any]:
    validated = _validate_selector(selector)
    revision_id = validated.get("revision_id")
    if not isinstance(revision_id, str) or revision_id != expected_revision_id:
        raise TopologyResolutionError("selector revision does not match the opened FCStd")
    obj = document.getObject(validated["object_name"])
    if obj is None or getattr(obj, "Shape", None) is None:
        raise TopologyResolutionError("selector object has no resolvable shape")
    if validated["subelement_kind"] == "face":
        return _resolve_planar_face(obj, validated)
    return _resolve_circular_edge(obj, validated)
