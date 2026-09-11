from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.freecad.contracts import FreeCADOperationPlan


def _cylinder_plan() -> dict:
    return {
        "schema_version": "freecad-operation-plan.v1",
        "document_name": "CylinderModel",
        "operations": [
            {
                "op_id": "create-sketch",
                "action": "sketch.create",
                "args": {"name": "BaseSketch", "plane": "xy"},
            },
            {
                "op_id": "add-circle",
                "action": "sketch.add_geometry",
                "args": {
                    "sketch": "BaseSketch",
                    "geometry": {
                        "kind": "circle",
                        "center": {"x": 5, "y": 5},
                        "radius_mm": 5,
                    },
                },
            },
            {
                "op_id": "lock-circle-x",
                "action": "sketch.add_constraint",
                "args": {
                    "sketch": "BaseSketch",
                    "kind": "distance_x",
                    "first": {"geometry_index": 0, "point_position": 3},
                    "value_mm": 5,
                },
            },
            {
                "op_id": "lock-circle-y",
                "action": "sketch.add_constraint",
                "args": {
                    "sketch": "BaseSketch",
                    "kind": "distance_y",
                    "first": {"geometry_index": 0, "point_position": 3},
                    "value_mm": 5,
                },
            },
            {
                "op_id": "lock-circle-radius",
                "action": "sketch.add_constraint",
                "args": {
                    "sketch": "BaseSketch",
                    "kind": "radius",
                    "first": {"geometry_index": 0},
                    "value_mm": 5,
                },
            },
            {
                "op_id": "pad-cylinder",
                "action": "feature.pad",
                "args": {
                    "name": "Pad",
                    "profile": "BaseSketch",
                    "length_mm": 10,
                },
            },
            {
                "op_id": "export-model",
                "action": "document.export",
                "args": {
                    "formats": ["fcstd", "step", "stl"],
                    "basename": "cylinder",
                },
            },
        ],
    }


def test_freecad_operation_plan_normalizes_typed_args() -> None:
    plan = FreeCADOperationPlan.model_validate(_cylinder_plan())

    assert plan.schema_version == "freecad-operation-plan.v1"
    assert plan.operations[1].typed_args().geometry.kind == "circle"
    assert plan.operations[-1].typed_args().formats == ("fcstd", "step", "stl")


def test_freecad_plan_requires_one_final_export() -> None:
    payload = _cylinder_plan()
    payload["operations"] = payload["operations"][:-1]

    with pytest.raises(ValidationError, match="final document.export"):
        FreeCADOperationPlan.model_validate(payload)


def test_freecad_plan_rejects_unknown_action_args() -> None:
    payload = _cylinder_plan()
    payload["operations"][0]["args"]["python"] = "import os"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FreeCADOperationPlan.model_validate(payload)


def test_freecad_plan_rejects_raw_face_selection() -> None:
    payload = _cylinder_plan()
    payload["operations"].insert(
        -1,
        {
            "op_id": "unsafe-fillet",
            "action": "feature.fillet",
            "args": {
                "name": "Fillet",
                "target": "Pad",
                "radius_mm": 1,
                "edges": ["Edge1"],
            },
        },
    )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FreeCADOperationPlan.model_validate(payload)


def test_freecad_operation_ids_are_unique() -> None:
    payload = _cylinder_plan()
    payload["operations"][1]["op_id"] = payload["operations"][0]["op_id"]

    with pytest.raises(ValidationError, match="operation IDs must be unique"):
        FreeCADOperationPlan.model_validate(payload)
