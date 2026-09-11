from __future__ import annotations

import json
import math
import os
import shutil
from pathlib import Path

import pytest

from app.execution.capability_adapter import CapabilityExecutionAdapter
from app.execution.contracts import ExecutionStatus
from app.execution.podman_backend import PodmanExecutionBackend
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.operation_compiler import compile_common_generation


# Regression: FUSION-001..003 — Hole/Pocket could succeed without removing material.
# Found by the 2026-09-04 independent fusion acceptance review.
# Report: /Users/wentao/Desktop/CAD-Agent-重新融合系统核验报告-2026-09-04.md

OUTPUTS = {
    "fcstd": "application/vnd.freecad.fcstd",
    "state": "application/json",
    "step": "model/step",
    "stl": "model/stl",
}


def _operation(op_id: str, action: str, args: dict) -> dict:
    return {"op_id": op_id, "action": action, "args": args}


def _circle_operations(prefix: str, sketch: str, radius: float) -> list[dict]:
    return [
        _operation(
            f"{prefix}-geometry",
            "sketch.add_geometry",
            {
                "sketch": sketch,
                "geometry": {
                    "kind": "circle",
                    "center": {"x": 5, "y": 5},
                    "radius_mm": radius,
                },
            },
        ),
        _operation(
            f"{prefix}-x",
            "sketch.add_constraint",
            {
                "sketch": sketch,
                "kind": "distance_x",
                "first": {"geometry_index": 0, "point_position": 3},
                "value_mm": 5,
            },
        ),
        _operation(
            f"{prefix}-y",
            "sketch.add_constraint",
            {
                "sketch": sketch,
                "kind": "distance_y",
                "first": {"geometry_index": 0, "point_position": 3},
                "value_mm": 5,
            },
        ),
        _operation(
            f"{prefix}-radius",
            "sketch.add_constraint",
            {
                "sketch": sketch,
                "kind": "radius",
                "first": {"geometry_index": 0},
                "value_mm": radius,
            },
        ),
    ]


def _plan(name: str, operations: list[dict]) -> dict:
    return FreeCADOperationPlan.model_validate(
        {
            "schema_version": "freecad-operation-plan.v1",
            "document_name": name,
            "operations": operations,
        }
    ).model_dump(mode="json")


def _base_plan() -> dict:
    return _plan(
        "SubtractiveSemantics",
        [
            _operation(
                "create-base-sketch",
                "sketch.create",
                {"name": "BaseSketch", "plane": "xy"},
            ),
            *_circle_operations("base-circle", "BaseSketch", 5),
            _operation(
                "pad-base",
                "feature.pad",
                {"name": "Pad", "profile": "BaseSketch", "length_mm": 10},
            ),
            _operation(
                "export-base",
                "document.export",
                {"formats": ["fcstd", "step", "stl"], "basename": "base"},
            ),
        ],
    )


def _subtractive_plan(*, action: str, reversed_sketch: bool) -> dict:
    feature = "Hole" if action == "feature.hole" else "Pocket"
    args = {
        "name": feature,
        "profile": "CutSketch",
        "through_all": True,
    }
    if action == "feature.hole":
        args["diameter_mm"] = 4
    return _plan(
        "SubtractiveSemantics",
        [
            _operation(
                "create-cut-sketch",
                "sketch.create",
                {
                    "name": "CutSketch",
                    "plane": "xy",
                    "offset_mm": 10,
                    "reversed": reversed_sketch,
                },
            ),
            *_circle_operations("cut-circle", "CutSketch", 2),
            _operation("subtract-material", action, args),
            _operation(
                "export-result",
                "document.export",
                {"formats": ["fcstd", "step", "stl"], "basename": "result"},
            ),
        ],
    )


async def _execute(
    adapter: CapabilityExecutionAdapter,
    plan: dict,
    *,
    request_id: str,
    base: Path | None = None,
):
    revision_id = "subtractive-base" if base else None
    return await adapter.execute(
        capability="freecad",
        operation="execute",
        request_id=request_id,
        params={
            "plan": plan,
            **({"expected_revision_id": revision_id} if revision_id else {}),
        },
        inputs={"base": base} if base else {},
        artifact_media_type="application/vnd.freecad.fcstd",
        mode="3d",
        timeout_seconds=180,
        output_bytes=128 * 1024 * 1024,
        expected_base_revision_id=revision_id,
        declared_outputs=OUTPUTS,
    )


def test_compiled_plate_places_hole_profile_on_the_material_facing_side() -> None:
    plan = compile_common_generation(
        {
            "part_type": "plate",
            "dimensions": {"width": 100, "height": 60, "depth": 10},
            "features": [
                "through_hole:diameter=6,count=1,position=centered",
                "plate:thickness=10",
            ],
            "constraints": ["hole_diameter = 6", "hole_centered_on_face"],
        },
        output_formats=("step", "stl"),
    )

    assert plan is not None
    hole_sketch = next(
        operation.typed_args()
        for operation in plan.operations
        if operation.op_id == "create-hole-sketch"
    )
    assert hole_sketch.offset_mm == 10
    assert hole_sketch.reversed is False


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_REAL_FREECAD") != "1",
    reason="set RUN_REAL_FREECAD=1 to exercise the real FreeCAD sandbox",
)
async def test_real_subtractive_features_change_solid_or_fail_closed() -> None:
    backend = PodmanExecutionBackend(os.environ["SANDBOX_IMAGE"])
    adapter = CapabilityExecutionAdapter(backend)
    work_dirs: list[Path] = []
    try:
        generated = await _execute(adapter, _base_plan(), request_id="subtract-base")
        assert generated.execution.result.status is ExecutionStatus.SUCCEEDED
        work_dirs.append(generated.execution.work_dir)
        base = generated.execution.files["fcstd"]

        no_effect = await _execute(
            adapter,
            _subtractive_plan(action="feature.hole", reversed_sketch=True),
            request_id="subtract-no-effect",
            base=base,
        )
        assert no_effect.execution.result.status is ExecutionStatus.FAILED
        assert no_effect.execution.result.error is not None
        assert no_effect.execution.result.error.code == "subtractive_feature_no_effect"
        assert no_effect.execution.files == {}

        expected_volume = math.pi * (5**2 - 2**2) * 10
        for action in ("feature.hole", "feature.pocket"):
            outcome = await _execute(
                adapter,
                _subtractive_plan(action=action, reversed_sketch=False),
                request_id=f"subtract-{action}",
                base=base,
            )
            assert outcome.execution.result.status is ExecutionStatus.SUCCEEDED, (
                outcome.execution.result.error
            )
            work_dirs.append(outcome.execution.work_dir)
            state = json.loads(
                outcome.execution.files["state"].read_text(encoding="utf-8")
            )
            feature_name = "Hole" if action == "feature.hole" else "Pocket"
            feature = next(
                item for item in state["objects"] if item["name"] == feature_name
            )
            pad = next(item for item in state["objects"] if item["name"] == "Pad")
            assert pad["shape"]["volume"] == pytest.approx(math.pi * 5**2 * 10)
            assert feature["shape"]["volume"] == pytest.approx(expected_volume)
            assert feature["shape"]["volume"] < pad["shape"]["volume"]
            assert feature["shape"]["faces"] > pad["shape"]["faces"]
    finally:
        for work_dir in work_dirs:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("RUN_REAL_FREECAD") != "1", reason="requires real FreeCAD")
async def test_real_through_hole_survives_thickness_changes_and_reopen() -> None:
    """Q01: a successful recompute must not cap a previously through-all hole."""
    adapter = CapabilityExecutionAdapter(PodmanExecutionBackend(os.environ["SANDBOX_IMAGE"]))
    plan = compile_common_generation(
        {
            "part_type": "plate",
            "dimensions": {"width": 100, "height": 60, "depth": 10},
            "features": ["through_hole:diameter=6,count=1,position=centered"],
        },
        output_formats=("step", "stl"),
    )
    assert plan is not None
    work_dirs = []
    try:
        generated = await _execute(adapter, plan.model_dump(mode="json"), request_id="through-base")
        assert generated.execution.result.status is ExecutionStatus.SUCCEEDED
        work_dirs.append(generated.execution.work_dir)
        base = generated.execution.files["fcstd"]
        # Optionally re-open the pre-fix acceptance artifact as well as a new model.
        bases = [base]
        if os.getenv("LEGACY_FREECAD_FIXTURE"):
            bases.append(Path(os.environ["LEGACY_FREECAD_FIXTURE"]))
        for index, original in enumerate(bases):
            base = original
            for thickness in (12, 6, 15):
                modified = await _execute(
                    adapter,
                    _plan("Model", [
                        _operation(f"thickness-{thickness}", "property.set", {
                            "object": "Pad", "property": "Length", "value": thickness,
                            "expected_property_type": "App::PropertyLength", "unit": "mm",
                        }),
                        _operation(f"export-{thickness}", "document.export", {
                            "formats": ["fcstd", "step", "stl"], "basename": "modified",
                        }),
                    ]),
                    request_id=f"through-{index}-{thickness}",
                    base=base,
                )
                assert modified.execution.result.status is ExecutionStatus.SUCCEEDED, modified.execution.result.error
                work_dirs.append(modified.execution.work_dir)
                state = json.loads(modified.execution.files["state"].read_text())
                hole = next(obj for obj in state["objects"] if obj["name"] == "Hole")
                assert hole["shape"]["volume"] == pytest.approx((100 * 60 - math.pi * 3**2) * thickness)
                import trimesh
                mesh = trimesh.load(modified.execution.files["stl"], force="mesh")
                assert mesh.is_watertight
                assert mesh.euler_number == 0  # one through-hole, not a sealed cap
                assert mesh.extents.tolist() == pytest.approx([100, 60, thickness])
                base = modified.execution.files["fcstd"]
    finally:
        for work_dir in work_dirs:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)
