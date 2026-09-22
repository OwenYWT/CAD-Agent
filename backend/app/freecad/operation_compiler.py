"""Deterministic compiler for common parametric FreeCAD operation plans."""

from __future__ import annotations

from app.freecad.edge_intent import requested_edge_scope

import hashlib
import json
import math
import re
from typing import Any

from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.operation_identity import scope_plan_to_base


_NUMBER = re.compile(
    r"(?:diameter|diam|Ø|直径)\s*[=:：]?\s*(\d+(?:\.\d+)?)",
    re.I,
)
_CHAMFER = re.compile(
    r"(?:chamfer|倒角)\s*(?:[:：,]\s*)?(?:size|尺寸)?\s*[=:：]?\s*"
    r"(\d+(?:\.\d+)?)",
    re.I,
)
_LEADING_CHAMFER = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:mm)?\s*(?:chamfer|倒角)",
    re.I,
)
_FILLET = re.compile(
    r"(?:fillet|圆角)\s*(?:[:：,]\s*)?(?:radius|半径)?\s*[=:：]?\s*"
    r"(\d+(?:\.\d+)?)",
    re.I,
)
_REVOLVED_CYLINDER = re.compile(
    r"^revolve_profile:circle_diameter=(\d+(?:\.\d+)?),"
    r"height=(\d+(?:\.\d+)?),centered$",
    re.I,
)
_BASE_CYLINDER = re.compile(
    r"^base_cylinder:diameter=(\d+(?:\.\d+)?),"
    r"height=(\d+(?:\.\d+)?)$",
    re.I,
)
_SOLID_CYLINDER = re.compile(
    r"^cylinder:solid,diameter=(\d+(?:\.\d+)?),"
    r"height=(\d+(?:\.\d+)?)$",
    re.I,
)
_CYLINDER_DIMENSION_CONSTRAINT = re.compile(
    r"^(diameter|height)\s*=\s*(\d+(?:\.\d+)?)\s*(?:mm)?$",
    re.I,
)


def _op(op_id: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"op_id": op_id, "action": action, "args": args}


def _constraint(
    op_id: str,
    sketch: str,
    kind: str,
    first: dict[str, int],
    *,
    second: dict[str, int] | None = None,
    value_mm: float | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "sketch": sketch,
        "kind": kind,
        "first": first,
    }
    if second is not None:
        args["second"] = second
    if value_mm is not None:
        args["value_mm"] = value_mm
    return _op(op_id, "sketch.add_constraint", args)


def _rectangle_constraints(
    sketch: str,
    *,
    width: float,
    depth: float,
    offset: float,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for index in (0, 2):
        operations.append(
            _constraint(
                f"base-horizontal-{index}",
                sketch,
                "horizontal",
                {"geometry_index": index},
            )
        )
    for index in (1, 3):
        operations.append(
            _constraint(
                f"base-vertical-{index}",
                sketch,
                "vertical",
                {"geometry_index": index},
            )
        )
    for index in range(4):
        operations.append(
            _constraint(
                f"base-coincident-{index}",
                sketch,
                "coincident",
                {"geometry_index": index, "point_position": 2},
                second={
                    "geometry_index": (index + 1) % 4,
                    "point_position": 1,
                },
            )
        )
    operations.extend(
        [
            _constraint(
                "base-width",
                sketch,
                "distance",
                {"geometry_index": 0},
                value_mm=width,
            ),
            _constraint(
                "base-depth",
                sketch,
                "distance",
                {"geometry_index": 1},
                value_mm=depth,
            ),
            _constraint(
                "base-origin-x",
                sketch,
                "distance_x",
                {"geometry_index": 0, "point_position": 1},
                value_mm=offset,
            ),
            _constraint(
                "base-origin-y",
                sketch,
                "distance_y",
                {"geometry_index": 0, "point_position": 1},
                value_mm=offset,
            ),
        ]
    )
    return operations


def _centered_hole_diameter(features: list[str]) -> float | None:
    for feature in features:
        lowered = feature.lower()
        if "hole" not in lowered and "孔" not in feature:
            continue
        match = _NUMBER.search(feature)
        if match:
            return float(match.group(1))
    return None


def _plate_dimensions(dimensions: dict[str, float]) -> tuple[float, float, float] | None:
    primary = {key: value for key, value in dimensions.items()
               if key not in {"hole_diameter", "fillet_radius"}}
    # Consume one complete coordinate convention. Never sort dimensions or
    # silently discard coordinates, offsets, a second hole diameter, etc.
    for keys in (("length", "width", "thickness"), ("width", "height", "depth"),
                 ("width", "depth", "thickness")):
        if set(primary) == set(keys):
            return tuple(primary[key] for key in keys)
    return None


def _feature_fields(value: str) -> tuple[str, dict[str, str]] | None:
    kind, separator, tail = value.lower().partition(":")
    if not separator:
        return None
    fields: dict[str, str] = {}
    for item in tail.split(","):
        key, equals, field = item.strip().partition("=")
        key, field = key.strip(), field.strip()
        if not equals or not field or key in fields:
            return None
        fields[key] = field
    return kind.strip(), fields


def _plate_semantics_covered(dimensions: dict[str, float], features: list[str],
                             constraints: list[str], size: tuple[float, float, float]) -> bool:
    """Closed grammar: every feature, field and constraint must be accounted for.

    A declined compilation proceeds through the full typed LLM generator. This
    shortcut supports only a rectangular plate and one centered through-hole.
    """
    width, depth, height = size
    hole_diameter = None
    fillet_radius = None
    for feature in features:
        parsed = _feature_fields(feature)
        if parsed is None:
            return False
        kind, fields = parsed
        if kind == "through_hole":
            if set(fields) not in ({"diameter", "count", "position"}, {"diameter", "position"}) or hole_diameter is not None:
                return False
            if fields.get("count", "1") != "1" or fields["position"] != "centered":
                return False
            try:
                hole_diameter = float(fields["diameter"])
            except ValueError:
                return False
            if not math.isfinite(hole_diameter) or not 0 < hole_diameter < min(width, depth):
                return False
        elif kind == "fillet":
            if set(fields) != {"radius", "edges"} or fields["edges"] != "all_outer" or fillet_radius is not None:
                return False
            try:
                fillet_radius = float(fields["radius"])
            except ValueError:
                return False
            if not math.isfinite(fillet_radius) or not 0 < fillet_radius < min(size) / 2:
                return False
        elif kind in {"plate", "base_plate", "rectangular_plate", "extrude", "extruded", "extrusion"}:
            expected = ({"length": width, "width": depth, "thickness": height}
                        if "length" in fields else {"width": width, "depth": depth, "height": height, "thickness": height})
            if fields.get("profile", "rectangle") != "rectangle":
                return False
            for key, value in fields.items():
                if key == "profile":
                    continue
                try:
                    if key not in expected or not math.isclose(float(value), expected[key]):
                        return False
                except ValueError:
                    return False
        else:
            return False
    for key, actual in (("hole_diameter", hole_diameter), ("fillet_radius", fillet_radius)):
        if key in dimensions and (actual is None or not math.isclose(dimensions[key], actual)):
            return False
    values = {**dimensions, "plate_width": depth, "plate_length": width,
              "plate_thickness": height, "hole_diameter": hole_diameter}
    for constraint in constraints:
        text = constraint.strip().lower()
        if text in {"hole_centered_on_face", "hole_centered_on_plate", "hole_centered=true", "hole_position=centered"}:
            if hole_diameter is None:
                return False
            continue
        if text == "no_unrequested_fillets_or_chamfers=true":
            if fillet_radius is not None:
                return False
            continue
        if text in {"fully_constrained_sketch=true", "sketch_fully_constrained=true"}:
            continue
        match = re.fullmatch(r"([a-z_]+)\s*(=|<|>)\s*([a-z_]+|\d+(?:\.\d+)?)(?:\s*mm)?", text)
        if match is None:
            return False
        left = values.get(match[1])
        right = values.get(match[3]) if match[3][0].isalpha() else float(match[3])
        if left is None or right is None:
            return False
        if not {"=": math.isclose(left, right), "<": left < right, ">": left > right}[match[2]]:
            return False
    return True


def _semantic_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:12]}"


def _state_objects(base_state: dict[str, Any]) -> list[dict[str, Any]]:
    if base_state.get("schema_version") not in {
        "freecad-state.v1",
        "freecad-state.v2",
    }:
        return []
    objects = base_state.get("objects")
    if not isinstance(objects, list):
        return []
    return [item for item in objects if isinstance(item, dict)]


def _find_property_target(
    key: str,
    objects: list[dict[str, Any]],
) -> tuple[str, str] | None:
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", key.lower())
    aliases: list[tuple[tuple[str, ...], str, str]] = [
        (("hole", "diameter"), "PartDesign::Hole", "Diameter"),
        (("孔", "直径"), "PartDesign::Hole", "Diameter"),
        (("pad", "length"), "PartDesign::Pad", "Length"),
        (("plate", "thickness"), "PartDesign::Pad", "Length"),
        (("厚度",), "PartDesign::Pad", "Length"),
        (("pocket", "depth"), "PartDesign::Pocket", "Length"),
        (("fillet", "radius"), "PartDesign::Fillet", "Radius"),
        (("chamfer", "size"), "PartDesign::Chamfer", "Size"),
    ]
    for terms, type_id, property_name in aliases:
        if not all(term in normalized for term in terms):
            continue
        candidates = [
            item
            for item in objects
            if item.get("type_id") == type_id
            and property_name in dict(item.get("properties") or {})
        ]
        if len(candidates) == 1:
            return str(candidates[0]["name"]), property_name

    split = [part for part in re.split(r"[.:/]", key) if part]
    if len(split) == 2:
        object_name, property_name = split
        for item in objects:
            properties = dict(item.get("properties") or {})
            if item.get("name") == object_name and property_name in properties:
                return object_name, property_name

    candidates: list[tuple[str, str]] = []
    for item in objects:
        for property_name in dict(item.get("properties") or {}):
            property_normalized = re.sub(r"[^a-z0-9]", "", property_name.lower())
            if normalized == property_normalized:
                candidates.append((str(item.get("name")), property_name))
    return candidates[0] if len(candidates) == 1 else None


def _body_tip(objects: list[dict[str, Any]]) -> str | None:
    names = {str(item.get("name")) for item in objects}
    for item in objects:
        if item.get("type_id") != "PartDesign::Body":
            continue
        tip = dict(item.get("properties") or {}).get("Tip")
        if isinstance(tip, str) and tip in names:
            return tip
    shaped = [
        str(item.get("name"))
        for item in objects
        if str(item.get("type_id", "")).startswith("PartDesign::")
        and isinstance(item.get("shape"), dict)
        and int(dict(item["shape"]).get("solids") or 0) > 0
    ]
    return shaped[-1] if shaped else None


def _feature_size(features: list[str], pattern: re.Pattern[str]) -> float | None:
    for feature in features:
        match = pattern.search(feature)
        if match:
            return float(match.group(1))
    return None


def compile_common_modification(
    requirements: dict[str, Any],
    *,
    base_state: dict[str, Any],
    output_formats: tuple[str, ...],
) -> FreeCADOperationPlan | None:
    """Compile bounded property edits/features against an observed FreeCAD state."""
    objects = _state_objects(base_state)
    if not objects:
        return None
    formats = list(dict.fromkeys(("fcstd", *output_formats)))
    if set(formats) - {"fcstd", "step", "stl", "dxf"}:
        return None
    target_params = dict(requirements.get("target_params") or {})
    features = [str(item) for item in requirements.get("new_features") or ()]
    operations: list[dict[str, Any]] = []

    for key, raw_value in sorted(target_params.items()):
        value = float(raw_value)
        if not math.isfinite(value) or value <= 0:
            return None
        normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(key).lower())
        if "chamfer" in normalized or "倒角" in normalized:
            features.append(f"chamfer:size={value:g}")
            continue
        target = _find_property_target(str(key), objects)
        if target is None:
            return None
        object_name, property_name = target
        args = {"object": object_name, "property": property_name, "value": value}
        operations.append(
            _op(_semantic_id("set-property", args), "property.set", args)
        )

    chamfer_features = [
        item for item in features if "chamfer" in item.lower() or "倒角" in item
    ]
    unsupported_features = [item for item in features if item not in chamfer_features]
    if unsupported_features or len(chamfer_features) > 1:
        return None
    if chamfer_features:
        scope = requested_edge_scope(chamfer_features[0])
        if scope is None:
            return None
        size = _feature_size(chamfer_features, _CHAMFER) or _feature_size(
            chamfer_features,
            _LEADING_CHAMFER,
        )
        target = _body_tip(objects)
        if size is None or size <= 0 or target is None:
            return None
        existing_names = {str(item.get("name")) for item in objects}
        suffix = 1
        name = "Chamfer"
        while name in existing_names:
            suffix += 1
            name = f"Chamfer{suffix}"
        args = {
            "name": name,
            "target": target,
            "size_mm": size,
            "use_all_edges": False,
            "edge_scope": scope,
        }
        operations.append(_op(_semantic_id("add-chamfer", args), "feature.chamfer", args))

    if not operations:
        return None
    export_args = {"formats": formats, "basename": "model"}
    operations.append(
        _op(
            _semantic_id("export-modified", {"operations": operations, **export_args}),
            "document.export",
            export_args,
        )
    )
    return scope_plan_to_base(FreeCADOperationPlan.model_validate(
        {
            "schema_version": "freecad-operation-plan.v1",
            "document_name": str(base_state.get("document") or "Model"),
            "operations": operations,
        }
    ), base_state)


def compile_common_generation(
    requirements: dict[str, Any],
    *,
    output_formats: tuple[str, ...],
) -> FreeCADOperationPlan | None:
    """Compile supported primitives from actual dimensions; return None if unsafe."""
    part_type = str(requirements.get("part_type") or "").lower()
    try:
        dimensions = {str(key).lower(): float(value)
                      for key, value in dict(requirements.get("dimensions") or {}).items()}
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(value) or value <= 0 for value in dimensions.values()):
        return None
    features = [str(item) for item in requirements.get("features") or ()]
    constraints = [str(item) for item in requirements.get("constraints") or ()]
    formats = list(dict.fromkeys(("fcstd", *output_formats)))
    if set(formats) - {"fcstd", "step", "stl", "dxf"}:
        return None

    operations: list[dict[str, Any]]
    if part_type == "plate":
        plate_dimensions = _plate_dimensions(dimensions)
        if plate_dimensions is None or not _plate_semantics_covered(dimensions, features, constraints, plate_dimensions):
            return None
        width, depth, height = plate_dimensions
        fillet_features = [
            item for item in features if "fillet" in item.lower() or "圆角" in item
        ]
        unsupported = []
        for item in features:
            lowered = item.lower()
            is_metadata = bool(
                re.match(r"^(?:(?:base|rectangular)_)?plate:", lowered)
                or re.match(r"^(?:extrude|extruded|extrusion):", lowered)
                or lowered.startswith(("box:", "export:"))
            )
            if (
                "hole" not in lowered
                and "孔" not in item
                and item not in fillet_features
                and not is_metadata
            ):
                unsupported.append(item)
        if unsupported or len(fillet_features) > 1:
            return None
        offset = 10.0
        operations = [
            _op(
                "create-base-sketch",
                "sketch.create",
                {"name": "BaseSketch", "plane": "xy"},
            ),
            _op(
                "add-base-rectangle",
                "sketch.add_geometry",
                {
                    "sketch": "BaseSketch",
                    "geometry": {
                        "kind": "rectangle",
                        "corner": {"x": offset, "y": offset},
                        "width_mm": width,
                        "height_mm": depth,
                    },
                },
            ),
            *_rectangle_constraints(
                "BaseSketch",
                width=width,
                depth=depth,
                offset=offset,
            ),
            _op(
                "pad-base",
                "feature.pad",
                {
                    "name": "Pad",
                    "profile": "BaseSketch",
                    "length_mm": height,
                },
            ),
        ]
        if fillet_features:
            radius = _feature_size(fillet_features, _FILLET) or dimensions.get(
                "fillet_radius"
            )
            if radius is None or radius >= min(width, depth, height) / 2:
                return None
            operations.append(
                _op(
                    "fillet-base",
                    "feature.fillet",
                    {
                        "name": "Fillet",
                        "target": "Pad",
                        "radius_mm": radius,
                        "use_all_edges": True,
                    },
                )
            )
        hole_descriptions = [
            item
            for item in [*features, *constraints]
            if "hole" in item.lower() or "孔" in item
        ]
        diameter = _centered_hole_diameter(hole_descriptions)
        if diameter is None and hole_descriptions:
            diameter = dimensions.get("hole_diameter")
        if hole_descriptions and diameter is None:
            return None
        if diameter is not None:
            center_x = offset + width / 2
            center_y = offset + depth / 2
            operations.extend(
                [
                    _op(
                        "create-hole-sketch",
                        "sketch.create",
                        {
                            "name": "HoleSketch",
                            "plane": "xy",
                            "offset_mm": height,
                            # The sketch sits on the material's top face.  Its
                            # normal already points back through the solid for
                            # PartDesign Hole's through-all direction; reversing
                            # the attachment puts the cut outside the plate.
                            "reversed": False,
                        },
                    ),
                    _op(
                        "add-hole-circle",
                        "sketch.add_geometry",
                        {
                            "sketch": "HoleSketch",
                            "geometry": {
                                "kind": "circle",
                                "center": {"x": center_x, "y": center_y},
                                "radius_mm": diameter / 2,
                            },
                        },
                    ),
                    _constraint(
                        "hole-center-x",
                        "HoleSketch",
                        "distance_x",
                        {"geometry_index": 0, "point_position": 3},
                        value_mm=center_x,
                    ),
                    _constraint(
                        "hole-center-y",
                        "HoleSketch",
                        "distance_y",
                        {"geometry_index": 0, "point_position": 3},
                        value_mm=center_y,
                    ),
                    _constraint(
                        "hole-radius",
                        "HoleSketch",
                        "radius",
                        {"geometry_index": 0},
                        value_mm=diameter / 2,
                    ),
                    _op(
                        "cut-through-hole",
                        "feature.hole",
                        {
                            "name": "Hole",
                            "profile": "HoleSketch",
                            "diameter_mm": diameter,
                            "through_all": True,
                        },
                    ),
                ]
            )
    elif part_type in {"cylinder", "revolution"}:
        diameter = dimensions.get("diameter")
        radius = dimensions.get("radius") or (diameter / 2 if diameter else None)
        height = dimensions.get("height") or dimensions.get("length")
        if (
            diameter is not None
            and radius is not None
            and not math.isclose(radius, diameter / 2)
        ):
            return None
        unsupported_features: list[str] = []
        for feature in features:
            lowered = feature.lower()
            if lowered in {
                "fully_constrained_sketch:true",
                "sketch:fully_constrained",
            }:
                continue
            declared = (
                _REVOLVED_CYLINDER.fullmatch(lowered)
                or _BASE_CYLINDER.fullmatch(lowered)
                or _SOLID_CYLINDER.fullmatch(lowered)
            )
            if declared is None:
                unsupported_features.append(feature)
                continue
            declared_diameter = float(declared.group(1))
            declared_height = float(declared.group(2))
            if (
                diameter is None
                or height is None
                or not math.isclose(declared_diameter, diameter)
                or not math.isclose(declared_height, height)
            ):
                unsupported_features.append(feature)
        unsupported_constraints: list[str] = []
        for item in constraints:
            lowered = item.lower()
            if (
                "fully constrained" in lowered
                or "完全约束" in item
                or re.fullmatch(
                    r"sketch_fully_constrained\s*(?:=\s*true)?",
                    lowered,
                )
            ):
                continue
            declared_dimension = _CYLINDER_DIMENSION_CONSTRAINT.fullmatch(lowered)
            if declared_dimension is None:
                unsupported_constraints.append(item)
                continue
            expected = diameter if declared_dimension.group(1).lower() == "diameter" else height
            if expected is None or not math.isclose(
                float(declared_dimension.group(2)),
                expected,
            ):
                unsupported_constraints.append(item)
        if (
            not radius
            or not height
            or unsupported_features
            or unsupported_constraints
        ):
            return None
        operations = [
            _op(
                "create-base-sketch",
                "sketch.create",
                {"name": "BaseSketch", "plane": "xy"},
            ),
            _op(
                "add-base-circle",
                "sketch.add_geometry",
                {
                    "sketch": "BaseSketch",
                    "geometry": {
                        "kind": "circle",
                        "center": {"x": 10, "y": 10},
                        "radius_mm": radius,
                    },
                },
            ),
            _constraint(
                "base-center-x",
                "BaseSketch",
                "distance_x",
                {"geometry_index": 0, "point_position": 3},
                value_mm=10,
            ),
            _constraint(
                "base-center-y",
                "BaseSketch",
                "distance_y",
                {"geometry_index": 0, "point_position": 3},
                value_mm=10,
            ),
            _constraint(
                "base-radius",
                "BaseSketch",
                "radius",
                {"geometry_index": 0},
                value_mm=radius,
            ),
            _op(
                "pad-cylinder",
                "feature.pad",
                {
                    "name": "Pad",
                    "profile": "BaseSketch",
                    "length_mm": height,
                },
            ),
        ]
    else:
        return None

    operations.append(
        _op(
            "export-model",
            "document.export",
            {"formats": formats, "basename": "model"},
        )
    )
    return FreeCADOperationPlan.model_validate(
        {
            "schema_version": "freecad-operation-plan.v1",
            "document_name": "Model",
            "operations": operations,
        }
    )
