from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from app.execution.capability_adapter import CapabilityExecutionAdapter
from app.execution.contracts import ExecutionStatus
from app.execution.podman_backend import PodmanExecutionBackend
from app.freecad.contracts import FreeCADOperationPlan


OUTPUTS = {
    "fcstd": "application/vnd.freecad.fcstd",
    "state": "application/json",
    "step": "model/step",
    "stl": "model/stl",
}


def _operation(op_id: str, action: str, args: dict) -> dict:
    return {"op_id": op_id, "action": action, "args": args}


def _circle_operations(prefix: str, sketch: str, x: float, y: float, radius: float) -> list[dict]:
    return [
        _operation(
            f"{prefix}-geometry",
            "sketch.add_geometry",
            {
                "sketch": sketch,
                "geometry": {
                    "kind": "circle",
                    "center": {"x": x, "y": y},
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
                "value_mm": x,
            },
        ),
        _operation(
            f"{prefix}-y",
            "sketch.add_constraint",
            {
                "sketch": sketch,
                "kind": "distance_y",
                "first": {"geometry_index": 0, "point_position": 3},
                "value_mm": y,
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


def _cylinder_plan() -> dict:
    return _plan(
        "CylinderModel",
        [
            _operation(
                "create-base-sketch",
                "sketch.create",
                {"name": "BaseSketch", "plane": "xy"},
            ),
            *_circle_operations("base-circle", "BaseSketch", 5, 5, 5),
            _operation(
                "pad-cylinder",
                "feature.pad",
                {"name": "Pad", "profile": "BaseSketch", "length_mm": 10},
            ),
            _operation(
                "export-cylinder",
                "document.export",
                {"formats": ["fcstd", "step", "stl"], "basename": "cylinder"},
            ),
        ],
    )


async def _execute(
    adapter: CapabilityExecutionAdapter,
    plan: dict,
    *,
    request_id: str,
    base: Path | None = None,
    expected_revision_id: str | None = None,
):
    revision_id = expected_revision_id or ("revision-base" if base else None)
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


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_REAL_FREECAD") != "1",
    reason="set RUN_REAL_FREECAD=1 to exercise the real FreeCAD sandbox",
)
async def test_real_freecad_generate_reopen_modify_replay_and_rollback() -> None:
    backend = PodmanExecutionBackend(os.environ["SANDBOX_IMAGE"])
    adapter = CapabilityExecutionAdapter(backend)
    work_dirs: list[Path] = []
    try:
        generate_plan = _cylinder_plan()
        generated = await _execute(
            adapter,
            generate_plan,
            request_id="freecad-generate",
        )
        assert generated.execution.result.status is ExecutionStatus.SUCCEEDED, (
            generated.execution.result.error
        )
        work_dirs.append(generated.execution.work_dir)
        base = generated.execution.files["fcstd"]
        assert base.read_bytes().startswith(b"PK")
        assert generated.execution.files["step"].stat().st_size > 1000
        assert generated.execution.files["stl"].stat().st_size > 1000

        modified = await _execute(
            adapter,
            _plan(
                "CylinderModel",
                [
                    _operation(
                        "set-pad-length-20",
                        "property.set",
                        {"object": "Pad", "property": "Length", "value": 20},
                    ),
                    _operation(
                        "export-cylinder-20",
                        "document.export",
                        {
                            "formats": ["fcstd", "step", "stl"],
                            "basename": "cylinder_20",
                        },
                    ),
                ],
            ),
            request_id="freecad-modify",
            base=base,
        )
        assert modified.execution.result.status is ExecutionStatus.SUCCEEDED
        work_dirs.append(modified.execution.work_dir)
        state = json.loads(modified.execution.files["state"].read_text(encoding="utf-8"))
        pad = next(item for item in state["objects"] if item["name"] == "Pad")
        assert pad["properties"]["Length"] == "20.00 mm"
        assert pad["shape"]["volume"] == pytest.approx(1570.7963267948965)

        replayed = await _execute(
            adapter,
            generate_plan,
            request_id="freecad-replay",
            base=base,
        )
        assert replayed.execution.result.status is ExecutionStatus.SUCCEEDED
        work_dirs.append(replayed.execution.work_dir)
        statuses = {
            item["status"] for item in replayed.metadata["result"]["operations"]
        }
        assert statuses == {"replayed"}

        base_hash = hashlib.sha256(base.read_bytes()).hexdigest()
        failed = await _execute(
            adapter,
            _plan(
                "CylinderModel",
                [
                    _operation(
                        "set-pad-length-before-failure",
                        "property.set",
                        {"object": "Pad", "property": "Length", "value": 30},
                    ),
                    _operation(
                        "fail-on-missing-property",
                        "property.set",
                        {"object": "Pad", "property": "MissingProperty", "value": 1},
                    ),
                    _operation(
                        "never-export-failed-model",
                        "document.export",
                        {
                            "formats": ["fcstd", "step", "stl"],
                            "basename": "must_not_exist",
                        },
                    ),
                ],
            ),
            request_id="freecad-rollback",
            base=base,
        )
        assert failed.execution.result.status is ExecutionStatus.FAILED
        assert failed.execution.files == {}
        assert hashlib.sha256(base.read_bytes()).hexdigest() == base_hash
    finally:
        for work_dir in work_dirs:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_REAL_FREECAD") != "1",
    reason="set RUN_REAL_FREECAD=1 to exercise the real FreeCAD sandbox",
)
async def test_real_freecad_hole_pocket_fillet_and_chamfer() -> None:
    backend = PodmanExecutionBackend(os.environ["SANDBOX_IMAGE"])
    adapter = CapabilityExecutionAdapter(backend)
    work_dirs: list[Path] = []
    try:
        generated = await _execute(adapter, _cylinder_plan(), request_id="feature-base")
        assert generated.execution.result.status is ExecutionStatus.SUCCEEDED, (
            generated.execution.result.error
        )
        work_dirs.append(generated.execution.work_dir)
        base = generated.execution.files["fcstd"]

        hole_operations = [
                _operation(
                    "create-hole-sketch",
                    "sketch.create",
                    {
                        "name": "HoleSketch",
                        "plane": "xy",
                        "offset_mm": 10,
                        "reversed": False,
                    },
                ),
                *_circle_operations("hole-circle", "HoleSketch", 5, 5, 2),
                _operation(
                    "cut-hole",
                    "feature.hole",
                    {
                        "name": "Hole",
                        "profile": "HoleSketch",
                        "diameter_mm": 4,
                        "through_all": True,
                    },
                ),
        ]
        fillet_plan = _plan(
            "CylinderModel",
            [
                *hole_operations,
                _operation(
                    "fillet-hole-body",
                    "feature.fillet",
                    {"name": "Fillet", "target": "Hole", "radius_mm": 0.4},
                ),
                _operation(
                    "export-fillet-body",
                    "document.export",
                    {
                        "formats": ["fcstd", "step", "stl"],
                        "basename": "fillet_body",
                    },
                ),
            ],
        )
        filleted = await _execute(
            adapter,
            fillet_plan,
            request_id="feature-hole-fillet",
            base=base,
        )
        assert filleted.execution.result.status is ExecutionStatus.SUCCEEDED, (
            filleted.execution.result.error
        )
        work_dirs.append(filleted.execution.work_dir)
        fillet_state = json.loads(
            filleted.execution.files["state"].read_text(encoding="utf-8")
        )
        fillet_types = {item["type_id"] for item in fillet_state["objects"]}
        assert {"PartDesign::Hole", "PartDesign::Fillet"} <= fillet_types
        assert {item["id"] for item in fillet_state["parameters"]} == {
            "Pad.Length",
            "Hole.Diameter",
            "Fillet.Radius",
        }

        chamfered = await _execute(
            adapter,
            _plan(
                "CylinderModel",
                [
                    *hole_operations,
                    _operation(
                        "chamfer-hole-body",
                        "feature.chamfer",
                        {"name": "Chamfer", "target": "Hole", "size_mm": 0.2},
                    ),
                    _operation(
                        "export-chamfer-body",
                        "document.export",
                        {
                            "formats": ["fcstd", "step", "stl"],
                            "basename": "chamfer_body",
                        },
                    ),
                ],
            ),
            request_id="feature-hole-chamfer",
            base=base,
        )
        assert chamfered.execution.result.status is ExecutionStatus.SUCCEEDED, (
            chamfered.execution.result.error
        )
        work_dirs.append(chamfered.execution.work_dir)
        chamfer_state = json.loads(
            chamfered.execution.files["state"].read_text(encoding="utf-8")
        )
        chamfer_types = {item["type_id"] for item in chamfer_state["objects"]}
        assert {"PartDesign::Hole", "PartDesign::Chamfer"} <= chamfer_types
        assert {item["id"] for item in chamfer_state["parameters"]} == {
            "Pad.Length",
            "Hole.Diameter",
            "Chamfer.Size",
        }
        assert "Hole.BaseProfileType" not in {
            item["id"] for item in chamfer_state["parameters"]
        }
        assert "Hole.ThreadDepth" not in {
            item["id"] for item in chamfer_state["parameters"]
        }

        modified_chamfer = await _execute(
            adapter,
            _plan(
                "CylinderModel",
                [
                    _operation(
                        "set-chamfer-size",
                        "property.set",
                        {
                            "object": "Chamfer",
                            "property": "Size",
                            "value": 0.3,
                            "expected_property_type": (
                                "App::PropertyQuantityConstraint"
                            ),
                            "unit": "mm",
                        },
                    ),
                    _operation(
                        "export-modified-chamfer",
                        "document.export",
                        {
                            "formats": ["fcstd", "step", "stl"],
                            "basename": "chamfer_modified",
                        },
                    ),
                ],
            ),
            request_id="feature-chamfer-parameter-modify",
            base=chamfered.execution.files["fcstd"],
        )
        assert (
            modified_chamfer.execution.result.status
            is ExecutionStatus.SUCCEEDED
        ), modified_chamfer.execution.result.error
        work_dirs.append(modified_chamfer.execution.work_dir)
        modified_chamfer_state = json.loads(
            modified_chamfer.execution.files["state"].read_text(encoding="utf-8")
        )
        assert next(
            item["value"]
            for item in modified_chamfer_state["parameters"]
            if item["id"] == "Chamfer.Size"
        ) == pytest.approx(0.3)

        pocket_plan = _plan(
            "CylinderModel",
            [
                _operation(
                    "create-pocket-sketch",
                    "sketch.create",
                    {
                        "name": "PocketSketch",
                        "plane": "xy",
                        "offset_mm": 10,
                        "reversed": False,
                    },
                ),
                *_circle_operations("pocket-circle", "PocketSketch", 5, 5, 2),
                _operation(
                    "cut-pocket",
                    "feature.pocket",
                    {
                        "name": "Pocket",
                        "profile": "PocketSketch",
                        "through_all": True,
                    },
                ),
                _operation(
                    "export-pocket",
                    "document.export",
                    {
                        "formats": ["fcstd", "step", "stl"],
                        "basename": "pocket_body",
                    },
                ),
            ],
        )
        pocketed = await _execute(
            adapter,
            pocket_plan,
            request_id="feature-pocket",
            base=base,
        )
        assert pocketed.execution.result.status is ExecutionStatus.SUCCEEDED, (
            pocketed.execution.result.error
        )
        work_dirs.append(pocketed.execution.work_dir)
    finally:
        for work_dir in work_dirs:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_REAL_FREECAD") != "1",
    reason="set RUN_REAL_FREECAD=1 to exercise the real FreeCAD sandbox",
)
async def test_real_freecad_semantic_selector_targets_current_circular_edge() -> None:
    backend = PodmanExecutionBackend(os.environ["SANDBOX_IMAGE"])
    adapter = CapabilityExecutionAdapter(backend)
    work_dirs: list[Path] = []
    try:
        generated = await _execute(
            adapter,
            _cylinder_plan(),
            request_id="topology-selector-base",
        )
        assert generated.execution.result.status is ExecutionStatus.SUCCEEDED, (
            generated.execution.result.error
        )
        work_dirs.append(generated.execution.work_dir)
        revision_id = str(uuid4())
        selected = await _execute(
            adapter,
            _plan(
                "CylinderModel",
                [
                    _operation(
                        "chamfer-semantic-top-edge",
                        "feature.chamfer",
                        {
                            "name": "Chamfer",
                            "target": "Pad",
                            "size_mm": 0.4,
                            "use_all_edges": False,
                            "selector": {
                                "schema_version": "topology-selector.v1",
                                "backend": "freecad",
                                "revision_id": revision_id,
                                "object_name": "Pad",
                                "subelement_kind": "edge",
                                "geometry": "circular",
                                "axis": "z",
                                "extreme": "max",
                                "radius_mm": 5,
                            },
                        },
                    ),
                    _operation(
                        "export-semantic-chamfer",
                        "document.export",
                        {
                            "formats": ["fcstd", "step", "stl"],
                            "basename": "semantic_chamfer",
                        },
                    ),
                ],
            ),
            request_id="topology-selector-chamfer",
            base=generated.execution.files["fcstd"],
            expected_revision_id=revision_id,
        )
        assert selected.execution.result.status is ExecutionStatus.SUCCEEDED, (
            selected.execution.result.error
        )
        work_dirs.append(selected.execution.work_dir)
        state = json.loads(
            selected.execution.files["state"].read_text(encoding="utf-8")
        )
        assert any(item["type_id"] == "PartDesign::Chamfer" for item in state["objects"])
    finally:
        for work_dir in work_dirs:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)
