"""Q04: native history restoration uses immutable FCStd, never empty Python."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.workflows import activities, temporal, revision_inputs
from app.workflows.handlers import planning, native_generation, cad_execution
from app.workflows.agent_v2 import McadAgentWorkflowV2
from app.services import durable_submission as submission


def request_payload():
    return {
        **{name: str(uuid4()) for name in (
            "workflow_run_id", "tenant_id", "project_id", "principal_id",
            "branch_id", "expected_base_revision_id",
        )},
        "operation": "modify", "modeling_backend": "freecad",
        "objective": "恢复历史原生模型", "output_formats": ["step", "stl"],
        "revision_restore": {
            "source_revision_id": str(uuid4()),
            "source_artifact_id": str(uuid4()),
            "source_sha256": "a" * 64,
        },
    }


def test_native_restore_request_carries_immutable_source_separate_from_head():
    payload = request_payload()
    request = temporal.McadAgentWorkflowV2Request.model_validate(payload)
    assert str(request.revision_restore.source_revision_id) != str(request.expected_base_revision_id)
    assert request.existing_code is None
    assert request.temporal_payload()["revision_restore"]["source_sha256"] == "a" * 64


@pytest.mark.parametrize("change", [
    {"operation": "generate"},
    {"modeling_backend": "cadquery", "existing_code": "result = box(1,1,1)"},
    {"existing_code": "print('must not execute')"},
    {"structured_modification": {"expected_state_sha256": "b" * 64, "parameter_updates": [{"parameter_id": "Pad.Length", "value": 12}]}},
])
def test_restore_cannot_be_mixed_with_generation_code_or_parameter_edits(change):
    with pytest.raises(ValidationError):
        temporal.McadAgentWorkflowV2Request.model_validate({**request_payload(), **change})


def test_restore_plan_only_inspects_and_exports_with_source_scoped_operation_ids():
    request = temporal.McadAgentWorkflowV2Request.model_validate(request_payload())
    plan = activities._revision_restore_operation_plan(request)
    assert [op.action for op in plan.operations] == ["document.inspect", "document.export"]
    assert plan.operations[-1].args["formats"] == ["fcstd", "step", "stl"]
    assert all(request.revision_restore.source_revision_id.hex in op.op_id for op in plan.operations)


@pytest.mark.asyncio
async def test_exact_parameter_failure_cannot_authorize_provider_redesign(monkeypatch):
    from temporalio.exceptions import ApplicationError

    payload = request_payload()
    payload.pop("revision_restore")
    payload.update(objective="Set Fillet.Radius to exactly 4.5 mm", structured_modification={
        "expected_state_sha256": "b" * 64,
        "parameter_updates": [{"parameter_id": "Fillet.Radius", "value": 4.5}]},
        candidate_build_id=str(uuid4()), source_id=str(uuid4()), repair_index=1,
        original_step_key="modify-main", failure={"category":"cad_kernel",
            "error_code":"shape_check_failed", "error_message":"Self-intersecting wire"})
    def forbidden_transaction(*_args):
        pytest.fail("an exact parameter request must stop before preparing provider repair")
    monkeypatch.setattr(native_generation, "tenant_transaction", forbidden_transaction)
    with pytest.raises(ApplicationError) as caught:
        await activities.McadWorkflowActivities(backend=object()).agent_repair_operations(payload)
    assert caught.value.type == "native_edit_repair_forbidden"
    assert "Self-intersecting wire" in str(caught.value)


@pytest.fixture
def logical_activities(monkeypatch):
    class Transaction:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return None
    async def no_replay(*_args, **_kwargs):
        return None
    async def start(*_args, **_kwargs):
        return uuid4()
    async def complete(*_args, **kwargs):
        return kwargs["result"]
    monkeypatch.setattr(revision_inputs, "tenant_transaction", lambda *_: Transaction())
    monkeypatch.setattr(cad_execution, "tenant_transaction", lambda *_: Transaction())
    monkeypatch.setattr(planning, "tenant_transaction", lambda *_: Transaction())
    monkeypatch.setattr(planning, "_stored_agent_step_result", no_replay)
    monkeypatch.setattr(planning, "_start_agent_logical_step", start)
    monkeypatch.setattr(planning, "_complete_agent_logical_step", complete)
    from app.workflows.handlers import decomposition
    monkeypatch.setattr(decomposition, "tenant_transaction", lambda *_: Transaction())
    monkeypatch.setattr(decomposition, "_stored_agent_step_result", no_replay)
    monkeypatch.setattr(decomposition, "_start_agent_logical_step", start)
    monkeypatch.setattr(decomposition, "_complete_agent_logical_step", complete)
    return activities.McadWorkflowActivities(backend=object())


@pytest.mark.asyncio
async def test_restore_requirements_are_deterministic_without_model_provider(logical_activities):
    class NoProvider:
        async def requirements_modification(self, *_args):
            pytest.fail("historical restore must not ask an LLM to redesign the model")
    logical_activities.durable_planner = NoProvider()
    result = await logical_activities.agent_requirements(request_payload())
    assert result["requirements"]["modification_type"] == "revision_restore"
    assert result["base_state"] is None


@pytest.mark.asyncio
async def test_restore_plan_requires_confirmation_and_disables_all_model_repairs(logical_activities):
    payload = request_payload()
    result = await logical_activities.agent_plan({
        **payload, "backend_policy_v1": True,
        "requirements": {"description": "恢复历史原生模型", "modification_type": "revision_restore"},
    })
    assert result["requires_confirmation"] is True
    assert result["plan"]["modeling_backend"] == "freecad"
    assert all(gate["repair_budget"] == 0 for gate in result["plan"]["validation_policy"].values())
    assert result["plan"]["validation_policy"]["geometry"]["mode"] == "required"


@pytest.mark.asyncio
async def test_auto_3d_backend_uses_one_atomic_freecad_plan_even_with_many_constraints(logical_activities):
    async def unexpected_decomposition(_plan):
        raise AssertionError("atomic FreeCAD planning must not call the legacy decomposer")
    logical_activities.durable_planner.decompose_generation = unexpected_decomposition
    payload = request_payload()
    payload.pop("revision_restore")
    payload.update(operation="generate", modeling_backend="auto", objective="Four corner mounting holes")
    requirements = {"description": "Four corner holes", "part_type": "plate", "dimensions": {"length": 80, "width": 60, "thickness": 8},
                    "features": ["four holes"], "constraints": ["hole count 4", "offset 10", "no center hole"]}
    result = await logical_activities.agent_decompose({**payload, "requirements": requirements, "backend_policy_v1": True})
    assert result["skipped"] and result["decomposition"] is None
    planned = await logical_activities.agent_plan({**payload, "requirements": requirements, "backend_policy_v1": True})
    assert planned["plan"]["modeling_backend"] == "freecad"
    assert len(planned["plan"]["steps"]) == 1
    assert planned["plan"]["steps"][0]["output_formats"] == ["step", "stl"]


@pytest.mark.asyncio
async def test_activity_reads_historical_artifact_not_current_head(logical_activities, monkeypatch):
    payload = request_payload()
    request = temporal.McadAgentWorkflowV2Request.model_validate(payload)
    seen = []
    artifact = {"id": request.revision_restore.source_artifact_id, "sha256": "a" * 64}
    async def resolve(_connection, **kwargs):
        seen.append(kwargs)
        return artifact
    monkeypatch.setattr(revision_inputs, "committed_artifact_for_revision", resolve)
    assert await logical_activities._freecad_revision_artifact(request, "fcstd") == artifact
    assert seen[0]["revision_id"] == request.revision_restore.source_revision_id


@pytest.mark.asyncio
async def test_activity_rejects_artifact_identity_drift(logical_activities, monkeypatch):
    request = temporal.McadAgentWorkflowV2Request.model_validate(request_payload())
    async def resolve(_connection, **_kwargs):
        return {"id": uuid4(), "sha256": "a" * 64}
    monkeypatch.setattr(revision_inputs, "committed_artifact_for_revision", resolve)
    with pytest.raises(Exception, match="identity"):
        await logical_activities._freecad_revision_artifact(request, "fcstd")


@pytest.mark.asyncio
async def test_failed_restore_never_enters_ai_repair(monkeypatch):
    from temporalio.exceptions import ApplicationError
    flow = McadAgentWorkflowV2()
    calls = []
    async def activity(name, payload, **_kwargs):
        calls.append(name)
        if name == "agent_v2.generate_operations":
            return {"source_id": str(uuid4()), "source_hash": "a" * 64, "source_code": "{}"}
        if name == "agent_v2.execute_freecad":
            raise ApplicationError("historical document invalid", {"category": "cad_kernel"}, type="invalid_document", non_retryable=True)
        pytest.fail("must not repair a historical model")
    monkeypatch.setattr(flow, "_activity", activity)
    with pytest.raises(Exception, match="historical document invalid"):
        await flow._freecad_model_step(
            request=request_payload(), plan={"objective": "restore", "design_brief": {}}, candidate_build_id=str(uuid4()),
            step={"step_key": "restore-main"}, step_index=2,
            requirements={}, base_state=None,
        )
    assert calls == ["agent_v2.generate_operations", "agent_v2.execute_freecad"]


@pytest.fixture
def restore_service(monkeypatch):
    payload = request_payload()
    request = temporal.McadAgentWorkflowV2Request.model_validate(payload)
    state = SimpleNamespace(
        request=request, allowed=True, source=request.revision_restore.source_revision_id,
        artifact={"id": request.revision_restore.source_artifact_id, "sha256": "a" * 64},
        queries=[], starts=[], existing=None,
    )
    class Transaction:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def scalar(self, sql, params):
            state.queries.append((str(sql), params))
            return state.source
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: state.existing))
    async def permission(*_args, **_kwargs): return state.allowed
    async def artifact(*_args, **_kwargs): return state.artifact
    async def start(**kwargs):
        state.starts.append(kwargs)
        return request.workflow_run_id, object()
    monkeypatch.setattr(submission, "tenant_transaction", lambda *_: Transaction())
    monkeypatch.setattr(submission, "principal_has_permission", permission)
    monkeypatch.setattr(submission, "committed_artifact_for_revision", artifact)
    monkeypatch.setattr(submission, "start_mcad_agent_v2_workflow", start)
    state.principal = SimpleNamespace(tenant_id=request.tenant_id, principal_id=request.principal_id)
    state.identity = {
        "project_id": request.project_id, "branch_id": request.branch_id,
        "expected_base_revision_id": request.expected_base_revision_id,
    }
    return state


async def resolve(state):
    return await submission.resolve_native_revision_restore(
        state.principal, **state.identity,
        source_revision_id=state.request.revision_restore.source_revision_id, panel_id="q04-panel",
    )


@pytest.mark.asyncio
async def test_restore_resolver_checks_same_project_branch_panel_and_permission(restore_service):
    state = restore_service
    restore, context = await resolve(state)
    assert restore == state.request.revision_restore
    assert context.base_revision_id == state.request.expected_base_revision_id
    sql, params = state.queries[0]
    assert "r.project_id=:project_id" in sql and "r.branch_id=:branch_id" in sql
    assert "b.name=:branch_name" in sql
    assert params["source_revision_id"] == restore.source_revision_id
    assert params["branch_name"].startswith("panel-")
    state.allowed = False
    state.queries.clear()
    with pytest.raises(PermissionError): await resolve(state)
    assert state.queries == []


@pytest.mark.asyncio
async def test_restore_missing_foreign_or_empty_version_is_not_success(restore_service):
    state = restore_service
    state.source = None
    with pytest.raises(ValueError, match="当前工程面板"): await resolve(state)
    state.source = state.request.revision_restore.source_revision_id
    state.artifact = None
    with pytest.raises(ValueError, match="没有可恢复"): await resolve(state)
    assert state.starts == []


@pytest.mark.asyncio
async def test_restore_submission_hash_includes_source_and_replays_without_moving_head(restore_service, monkeypatch):
    from app.execution.canonical import canonical_sha256
    from app.services.run_state import IdempotencyConflict
    state = restore_service
    restore, context = await resolve(state)
    checked = []
    async def authorize(*_args, **kwargs): checked.append(kwargs)
    monkeypatch.setattr(submission, "_authorize_current_base", authorize)
    kwargs = {
        **state.identity, "idempotency_key": "q04-same-request", "operation": "modify",
        "objective": "恢复历史版本", "output_formats": ["step", "stl"],
        "modeling_backend": "freecad", "operation_context": context, "revision_restore": restore,
    }
    result = await submission.submit_durable_workflow(state.principal, **kwargs)
    assert result.workflow_run_id == state.request.workflow_run_id
    assert len(checked) == 1
    assert state.starts[0]["revision_restore"] == restore
    payload = temporal.mcad_agent_v2_request_payload(
        branch_id=state.request.branch_id, expected_base_revision_id=state.request.expected_base_revision_id,
        operation="modify", objective=kwargs["objective"], existing_code=None,
        manufacturing_profile=None, output_formats=("step", "stl"), confirmation_timeout_seconds=3600,
        modeling_backend="freecad", operation_context=context, revision_restore=restore,
    )
    state.existing = {
        "id": result.workflow_run_id, "project_id": state.request.project_id,
        "requested_by_principal_id": state.request.principal_id,
        "kind": "mcad.agent.v2.modify", "request_payload_hash": canonical_sha256(payload),
    }
    await submission.submit_durable_workflow(state.principal, **kwargs)
    assert len(checked) == 1  # idempotent replay still works after the head has moved
    assert state.starts[-1]["require_worker_ready"] is False
    changed = restore.model_copy(update={"source_revision_id": uuid4()})
    with pytest.raises(IdempotencyConflict):
        await submission.submit_durable_workflow(state.principal, **{**kwargs, "revision_restore": changed})
    assert len(state.starts) == 2


@pytest.mark.asyncio
async def test_restore_stale_base_creates_no_workflow(restore_service, monkeypatch):
    from app.repositories.revisions import StaleBaseRevision
    state = restore_service
    restore, context = await resolve(state)
    async def stale(*_args, **_kwargs): raise StaleBaseRevision("head moved")
    monkeypatch.setattr(submission, "_authorize_current_base", stale)
    with pytest.raises(StaleBaseRevision):
        await submission.submit_durable_workflow(
            state.principal, **state.identity, idempotency_key="q04-stale", operation="modify",
            objective="恢复历史版本", output_formats=["step", "stl"], modeling_backend="freecad",
            operation_context=context, revision_restore=restore,
        )
    assert state.starts == []


@pytest.mark.asyncio
async def test_restore_corrupt_bytes_never_reach_execution_backend(monkeypatch):
    request = temporal.McadAgentWorkflowV2Request.model_validate(request_payload())
    class Transaction:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def scalar(self, *_args, **_kwargs): return request.expected_base_revision_id
    async def no_replay(*_args, **_kwargs): return None
    async def artifact(*_args, **_kwargs):
        return {"id": request.revision_restore.source_artifact_id, "sha256": "a" * 64,
                "object_key": "test/historical.FCStd", "size_bytes": 40}
    async def corrupt(*_args, **_kwargs): return {"sha256": "b" * 64, "size_bytes": 40}
    monkeypatch.setattr(revision_inputs, "tenant_transaction", lambda *_: Transaction())
    monkeypatch.setattr(cad_execution, "tenant_transaction", lambda *_: Transaction())
    monkeypatch.setattr(planning, "tenant_transaction", lambda *_: Transaction())
    monkeypatch.setattr(cad_execution, "get_staging_manifest_for_step", no_replay)
    monkeypatch.setattr(revision_inputs, "committed_artifact_for_revision", artifact)
    monkeypatch.setattr(cad_execution, "download_object", corrupt)
    monkeypatch.setattr(activities.activity, "info", lambda: SimpleNamespace(activity_id="test", attempt=1))
    operation_plan = activities._revision_restore_operation_plan(request)
    source_code = operation_plan.model_dump_json()
    import hashlib
    from app.agent.durable_planner import DurableAgentPlanner
    from app.models.schemas import ModificationPlan
    plan = DurableAgentPlanner().compose_modification(
        "restore", ModificationPlan(description="restore", modification_type="revision_restore"),
        expected_base_revision_id=request.expected_base_revision_id,
    )
    with pytest.raises(Exception, match="integrity verification"):
        await activities.McadWorkflowActivities(backend=object()).agent_execute_freecad({
            **request.temporal_payload(), "plan": plan.temporal_payload(),
            "step": plan.steps[0].model_dump(mode="json"), "candidate_build_id": str(uuid4()),
            "source_code": source_code, "source_hash": hashlib.sha256(source_code.encode()).hexdigest(),
        })
