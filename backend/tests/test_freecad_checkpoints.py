"""Checkpoint contracts and workflow routing; provider inputs are controlled."""
import json

import pytest
from pydantic import ValidationError

from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.operation_generator import FreeCADOperationGenerator
from app.workflows.agent_v2 import McadAgentWorkflowV2
from tests.test_freecad_agent_tools import ProviderTurns, call
from tests.test_freecad_operation_generator import _provenance, _valid_plan


def checkpoint_plan():
    return {"execution_mode": "checkpoint", "operations": [
        {"op_id": "make", "action": "api.execute", "args": {
            "source": "document.addObject('Spreadsheet::Sheet', 'Dimensions')"}},
        {"op_id": "export", "action": "document.export", "args": {
            "formats": ["fcstd"], "objects": ["Dimensions"]}}]}


@pytest.mark.asyncio
async def test_checkpoint_allows_native_only_artifact_without_claiming_final_success():
    provider = ProviderTurns([call("freecad_execute", checkpoint_plan())])
    result = await FreeCADOperationGenerator(client=provider, provenance_reader=_provenance)._complete(
        user_payload={"checkpoint_enabled": True}, generator_kind="test",
        output_formats=("step", "stl"))
    assert result.operation_plan.execution_mode == "checkpoint"
    assert result.operation_plan.operations[-1].args["formats"] == ["fcstd"]
    assert "tool_result" not in result.provenance


@pytest.mark.asyncio
async def test_old_workflow_cannot_accidentally_accept_checkpoint():
    provider = ProviderTurns([call("freecad_execute", checkpoint_plan())],
                             [call("freecad_execute", _valid_plan())])
    result = await FreeCADOperationGenerator(client=provider, provenance_reader=_provenance)._complete(
        user_payload={}, generator_kind="test", output_formats=("step", "stl"))
    assert result.operation_plan.execution_mode == "final"
    replies = [m for m in provider.requests[-1]["messages"] if m["role"] == "tool"]
    assert "checkpoint execution is not enabled" in replies[0]["content"]


def test_read_only_checkpoint_cannot_start_no_progress_loop():
    with pytest.raises(ValidationError, match="document-changing"):
        FreeCADOperationPlan.model_validate({"execution_mode": "checkpoint", "operations": [
            {"op_id": "export", "action": "document.export", "args": {"formats": ["fcstd"]}}]})


@pytest.mark.asyncio
async def test_workflow_returns_only_final_and_threads_verified_checkpoint_identity():
    engine = McadAgentWorkflowV2()
    requests = []

    async def run_step(**kwargs):
        requests.append(kwargs)
        turn = kwargs["turn_index"]
        return {"generated": {"source_code": json.dumps({"execution_mode": "checkpoint" if turn < 2 else "final"})},
                "executed": {"staging_manifest_id": f"checkpoint-{turn}"}}

    engine._freecad_model_step = run_step
    result = await engine._freecad_tool_turns(request={"objective": "unchanged"})
    assert result["executed"]["staging_manifest_id"] == "checkpoint-2"
    assert "checkpoint_manifest_id" not in requests[0]["request"]
    assert requests[1]["request"]["checkpoint_manifest_id"] == "checkpoint-0"
    assert requests[2]["request"]["checkpoint_manifest_id"] == "checkpoint-1"
    assert all(r["request"]["objective"] == "unchanged" for r in requests)


def test_all_repair_paths_keep_the_same_checkpoint_base():
    # The failed candidate must not become the base of its own repair.
    value = {"checkpoint_manifest_id": "successful-intermediate",
             "executed": {"staging_manifest_id": "failed-final"}}
    assert McadAgentWorkflowV2._checkpoint_execution_context(value) == {
        "checkpoint_enabled": True, "checkpoint_manifest_id": "successful-intermediate"}


@pytest.mark.asyncio
async def test_cad_uses_leased_job_without_an_activity_wall_clock():
    engine=McadAgentWorkflowV2()
    engine._cad_jobs_v1=True
    calls=[]
    async def job(name,payload,suffix):
        calls.append((name,payload,suffix))
        return {'status':'succeeded'}
    engine._model_job=job
    from app.contracts.model_operations import CAD_OPERATIONS
    for operation in CAD_OPERATIONS:
        assert await engine._activity(operation,{},suffix='test',execution=True)=={'status':'succeeded'}
    assert {c[0] for c in calls}==CAD_OPERATIONS


def test_durable_execution_identity_uses_actual_lease_generation():
    from uuid import uuid4
    from app.model_job_context import ModelJobContext,model_job_context
    from app.workflows.execution_support import execution_identity, execution_timeout
    with pytest.raises(RuntimeError,match='leased durable job'):
        execution_identity()
    job=ModelJobContext(uuid4(),7)
    assert execution_timeout({'timeout_seconds':120},60)==120
    token=model_job_context.set(job)
    try:
        identity=execution_identity()
        assert identity.attempt==7 and str(job.job_id) in identity.activity_id
        assert execution_timeout({'timeout_seconds':120},60) is None
    finally:
        model_job_context.reset(token)
