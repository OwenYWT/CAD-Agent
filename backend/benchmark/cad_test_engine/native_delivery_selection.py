"""Select final native CAD objects without counting retained construction history."""

from __future__ import annotations

from typing import Any, Iterable


def _has_shape(obj: Any) -> bool:
    shape = getattr(obj, "Shape", None)
    return shape is not None and not shape.isNull()


def _is_shape_object(obj: Any) -> bool:
    type_id = str(getattr(obj, "TypeId", ""))
    return (
        type_id == "PartDesign::Body"
        or type_id == "App::Link"
        or bool(getattr(obj, "isDerivedFrom", lambda _kind: False)("Part::Feature"))
    )


def _is_visible(obj: Any) -> bool:
    view = getattr(obj, "ViewObject", None)
    return bool(getattr(view, "Visibility", True))


def select_final_objects(objects: Iterable[Any]) -> list[Any]:
    candidates = [obj for obj in objects if _is_shape_object(obj) and _has_shape(obj)]
    candidate_ids = {id(obj) for obj in candidates}
    roots = [
        obj
        for obj in candidates
        if not any(
            id(parent) in candidate_ids and parent is not obj
            for parent in (getattr(obj, "InList", None) or ())
        )
    ]
    visible_roots = [obj for obj in roots if _is_visible(obj)]
    return visible_roots or roots
