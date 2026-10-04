from __future__ import annotations

import json
import os
import shutil

import pytest

from app.agent.durable_planner import DurableAgentPlanner
from app.execution.capability_adapter import CapabilityExecutionAdapter
from app.execution.contracts import ExecutionStatus
from tests.native_runtime import native_backend
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

    backend = native_backend()
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


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("RUN_REAL_FREECAD_AGENT")!="1",reason="requires real LLM and FreeCAD")
async def test_real_provider_requests_l2_measurement_then_modifies_native_model():
    from tests.test_freecad_runtime import _cylinder_plan, _execute
    from tests.test_freecad_operation_generator import _plan
    from app.execution.composition import get_execution_backend
    adapter=CapabilityExecutionAdapter(get_execution_backend())
    paths=[]
    try:
        initial=await _execute(adapter,_cylinder_plan(),request_id='inspect-native-cylinder')
        assert initial.execution.result.status is ExecutionStatus.SUCCEEDED,initial.execution.result.error
        paths.append(initial.execution.work_dir)
        state=json.loads(initial.execution.files['state'].read_text())
        objective='Inspect the current Pad Length and BaseSketch constraints. Increase Pad Length by exactly 2 mm. Preserve the circle and all other features.'
        generated=await FreeCADOperationGenerator().generate(
            plan=_plan().model_copy(update={'operation':'modify','objective':objective}),
            requirements={'description':objective},base_state=state,output_formats=('step','stl'))
        assert generated.generator_kind=='freecad_operations'
        calls=generated.provenance['inspection_calls']
        assert calls and all(c['provider']['provider_response_id'] for c in calls)
        assert any(o.get('name')=='Pad' for c in calls for o in c['result']['objects'])
        modified=await _execute(adapter,generated.operation_plan.model_dump(mode='json'),
            request_id='inspect-modify-cylinder',base=initial.execution.files['fcstd'])
        paths.append(modified.execution.work_dir)
        assert modified.execution.result.status is ExecutionStatus.SUCCEEDED,modified.execution.result.error
        updated=json.loads(modified.execution.files['state'].read_text())
        p=next(p for p in updated['parameters'] if p['id']=='Pad.Length')
        assert p['value']==12
        before_sketch=next(o for o in state['objects'] if o['name']=='BaseSketch')
        after_sketch=next(o for o in updated['objects'] if o['name']=='BaseSketch')
        assert before_sketch['inspection']['constraints']==after_sketch['inspection']['constraints']
        assert before_sketch['inspection']['geometry']==after_sketch['inspection']['geometry']
        print('CAD_REAL_L2_REPORT='+json.dumps({'calls':calls,'final_length_mm':p['value']},ensure_ascii=False))
    finally:
        for path in paths:
            if path is not None:
                shutil.rmtree(path,ignore_errors=True)
