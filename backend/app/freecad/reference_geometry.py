"""Semantic projection of native datum geometry, including legacy checkpoints.

These FreeCAD types describe axes/planes rather than bounded physical material.
Their OCC visualization shapes must not supply lengths, areas or mass centres.
Names and numeric magnitudes deliberately play no part in this classification.
"""
from __future__ import annotations

from typing import Any


REFERENCE_KINDS = {
    "App::Line": "axis", "App::Plane": "plane",
    "PartDesign::Line": "axis", "PartDesign::Plane": "plane",
}


def normalize_reference_object(obj: dict[str, Any]) -> dict[str, Any]:
    kind = REFERENCE_KINDS.get(obj.get("type_id"))
    if kind is None:
        return obj
    result = {key: value for key, value in obj.items() if key != "shape"}
    result["reference_geometry"] = {
        "kind": kind, "extent": "unbounded", "coordinate_system": "object_placement",
    }
    result["inspection"] = {
        **obj.get("inspection", {}),
        "topology": {"status": "not_applicable", "reason": "unbounded_reference_geometry",
                     "total": 0, "items": []},
    }
    return result


def normalize_reference_state(state: dict[str, Any]) -> dict[str, Any]:
    """Adapt in memory; never rewrite the immutable checkpoint or its digest."""
    if state.get("schema_version") not in {"freecad-state.v1", "freecad-state.v2"}:
        return state
    return {**state, "objects": [normalize_reference_object(obj) for obj in state.get("objects", [])]}
