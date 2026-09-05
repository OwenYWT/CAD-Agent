from __future__ import annotations

import json
import os
import shutil

import pytest

from app.agent.durable_planner import DurableAgentPlanner
from app.execution.capability_adapter import CapabilityExecutionAdapter
from app.execution.contracts import ExecutionStatus
from app.execution.podman_backend import PodmanExecutionBackend
from app.freecad.operation_generator import FreeCADOperationGenerator


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_REAL_FREECAD_AGENT") != "1",
    reason="set RUN_REAL_FREECAD_AGENT=1 for real LLM-to-FreeCAD integration",
)
async def test_real_natural_language_to_freecad_plate_with_hole() -> None:
    objective = (
        "Create one rectangular plate 100 mm long, 60 mm wide and 10 mm thick. "
        "Add one centered 6 mm diameter through hole. Export STEP and STL."
    )
    planner = DurableAgentPlanner()
    requirements = await planner.requirements_generation(objective)
    plan = planner.compose_freecad_generation(
        objective,
        requirements,
        output_formats=("step", "stl"),
    )
    try:
        generated = await FreeCADOperationGenerator().generate(
            plan=plan,
            requirements=requirements.model_dump(mode="json"),
            base_state=None,
            output_formats=("step", "stl"),
        )
    except Exception as exc:
        raise AssertionError(
            json.dumps(requirements.model_dump(mode="json"), ensure_ascii=False)
        ) from exc

    backend = PodmanExecutionBackend(os.environ["SANDBOX_IMAGE"])
    outcome = await CapabilityExecutionAdapter(backend).execute(
        capability="freecad",
        operation="execute",
        request_id="real-agent-plate-hole",
        params={"plan": generated.operation_plan.model_dump(mode="json")},
        inputs={},
        artifact_media_type="application/vnd.freecad.fcstd",
        mode="3d",
        timeout_seconds=180,
        output_bytes=128 * 1024 * 1024,
        declared_outputs={
            "fcstd": "application/vnd.freecad.fcstd",
            "state": "application/json",
            "step": "model/step",
            "stl": "model/stl",
        },
    )
    try:
        assert outcome.execution.result.status is ExecutionStatus.SUCCEEDED, (
            outcome.execution.result.model_dump(mode="json")
        )
        state = json.loads(
            outcome.execution.files["state"].read_text(encoding="utf-8")
        )
        assert any(
            item["type_id"] in {"PartDesign::Hole", "PartDesign::Pocket"}
            for item in state["objects"]
        )
        assert outcome.execution.files["fcstd"].read_bytes().startswith(b"PK")
        assert outcome.execution.files["step"].stat().st_size > 1000
        assert outcome.execution.files["stl"].stat().st_size > 1000
    finally:
        if outcome.execution.work_dir is not None:
            shutil.rmtree(outcome.execution.work_dir, ignore_errors=True)
