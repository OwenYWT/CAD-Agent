from types import SimpleNamespace
import hashlib
from uuid import uuid4

import pytest

from app.api import batch as batch_api
from app.api import changes as changes_api
from app.api import execute as execute_api
from app.api import generate as generate_api
from app.api import history as history_api
from app.api import websocket as websocket_api
from app.config import settings
from app.models.schemas import ExecuteRequest, GenerateRequest, GenerateResponse
from app.workflows.temporal import OperationContextV1
from fastapi import HTTPException


def _identity():
    return {
        "project_id": uuid4(),
        "branch_id": uuid4(),
        "expected_base_revision_id": uuid4(),
        "idempotency_key": f"cutover-{uuid4()}",
    }


@pytest.mark.parametrize("request_type", [GenerateRequest, batch_api.AsyncGenerateRequest])
@pytest.mark.parametrize("prompt", ["", "   ", "x" * 4001], ids=["empty", "whitespace", "over-limit"])
def test_prompt_contract_rejects_invalid_objectives_at_the_api_boundary(request_type, prompt):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        request_type(prompt=prompt, **_identity())


@pytest.mark.parametrize("request_type", [GenerateRequest, batch_api.AsyncGenerateRequest])
def test_prompt_contract_accepts_and_normalizes_boundary_length(request_type):
    assert request_type(prompt=" x ", **_identity()).prompt == "x"
    assert len(request_type(prompt="x" * 4000, **_identity()).prompt) == 4000


async def _no_rate_limit(*args, **kwargs):
    return None


def _success(identity, workflow_run_id):
    return GenerateResponse(
        request_id=str(workflow_run_id),
        success=True,
        files={
            "step": (
                f"/api/files/{workflow_run_id}/model-result.step"
            )
        },
        project_id=identity["project_id"],
        branch_id=identity["branch_id"],
        expected_base_revision_id=identity[
            "expected_base_revision_id"
        ],
        revision_id=uuid4(),
        workflow_run_id=workflow_run_id,
        change_set_id=uuid4(),
        task_status="succeeded",
    )


@pytest.mark.asyncio
async def test_generate_cutover_uses_only_durable_submission(monkeypatch):
    identity = _identity()
    workflow_run_id = uuid4()
    calls = []

    async def submit(principal, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(workflow_run_id=workflow_run_id)

    async def wait(principal, submission, timeout_seconds):
        return _success(identity, workflow_run_id)

    monkeypatch.setattr(generate_api.rate_limiter, "check", _no_rate_limit)
    monkeypatch.setattr(generate_api, "current_principal", lambda: object())
    monkeypatch.setattr(generate_api, "submit_durable_workflow", submit)
    monkeypatch.setattr(
        generate_api,
        "wait_for_compatibility_response",
        wait,
    )
    assert not hasattr(generate_api, "_get_orchestrator")

    response = await generate_api.generate(
        GenerateRequest(prompt="创建支架", **identity),
        SimpleNamespace(),
        None,
    )

    assert response.workflow_run_id == workflow_run_id
    assert len(calls) == 1
    assert calls[0] == {
        **identity,
        "operation": "generate",
        "objective": "创建支架",
        "output_formats": ["step", "stl"],
        "manufacturing_profile": None,
        "modeling_backend": "auto",
        "operation_context": OperationContextV1(
            rule="explicit_rest_operation",
            source_channel="rest",
            requested_operation="generate",
            resolved_operation="generate",
            submission_modeling_backend="auto",
            base_revision_id=identity["expected_base_revision_id"],
            base_source_kind="none",
        ),
    }


@pytest.mark.asyncio
async def test_execute_cutover_never_calls_process_local_executor(monkeypatch):
    identity = _identity()
    workflow_run_id = uuid4()
    calls = []

    async def submit(principal, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(workflow_run_id=workflow_run_id)

    async def wait(principal, submission, timeout_seconds):
        return _success(identity, workflow_run_id)

    monkeypatch.setattr(execute_api.rate_limiter, "check", _no_rate_limit)
    monkeypatch.setattr(execute_api, "current_principal", lambda: object())
    monkeypatch.setattr(execute_api, "submit_durable_workflow", submit)
    monkeypatch.setattr(
        execute_api,
        "wait_for_compatibility_response",
        wait,
    )
    assert not hasattr(execute_api, "_get_orchestrator")

    code = "result = box(2, 2, 2)"
    response = await execute_api.execute(
        ExecuteRequest(code=code, **identity),
        SimpleNamespace(),
        None,
    )

    assert response.workflow_run_id == workflow_run_id
    assert calls[0]["operation"] == "execute"
    assert calls[0]["code"] == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_kind", "expected_workflow_id"),
    [
        ("generate", "mcad.agent.v2.generate", "agent-v2"),
        ("modify", "mcad.agent.v2.modify", "agent-v2"),
        ("execute", "mcad.execute", "v1"),
    ],
)
async def test_durable_submission_uses_v2_only_for_new_agent_writes(
    monkeypatch,
    operation,
    expected_kind,
    expected_workflow_id,
):
    from app.services import durable_submission as submission_api

    identity = _identity()
    principal = SimpleNamespace(tenant_id=uuid4(), principal_id=uuid4())
    calls = []

    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(one_or_none=lambda: None)
            )

    async def authorize(*_args, **_kwargs):
        return None

    async def start_v2(**kwargs):
        calls.append(("agent-v2", kwargs))
        return uuid4(), object()

    async def start_v1(**kwargs):
        calls.append(("v1", kwargs))
        return uuid4(), object()

    monkeypatch.setattr(
        submission_api,
        "tenant_transaction",
        lambda *_args, **_kwargs: _Transaction(),
    )
    monkeypatch.setattr(submission_api, "_authorize_current_base", authorize)
    monkeypatch.setattr(
        submission_api,
        "start_mcad_agent_v2_workflow",
        start_v2,
        raising=False,
    )
    monkeypatch.setattr(submission_api, "start_mcad_workflow", start_v1)

    code = "result = box(2, 2, 2)" if operation != "generate" else None
    modify_context = (
        OperationContextV1(
            rule="explicit_rest_operation",
            source_channel="rest",
            requested_operation="modify",
            resolved_operation="modify",
            requested_modeling_backend="cadquery",
            submission_modeling_backend="cadquery",
            base_revision_id=identity["expected_base_revision_id"],
            base_source_kind="request_code",
            base_source_sha256=hashlib.sha256(code.encode()).hexdigest(),
        )
        if operation == "modify"
        else None
    )
    await submission_api.submit_durable_workflow(
        principal,
        **identity,
        operation=operation,
        objective="创建支架",
        output_formats=["step", "stl"],
        code=code,
        modeling_backend="cadquery" if operation == "modify" else None,
        operation_context=modify_context,
    )

    assert calls[0][0] == expected_workflow_id
    assert calls[0][1]["kind"] == expected_kind


@pytest.mark.asyncio
async def test_v2_idempotent_replay_repairs_start_without_readiness_gate(
    monkeypatch,
):
    """An accepted run can repair DB→Temporal crash windows during outage."""
    from app.services import durable_submission as submission_api

    identity = _identity()
    principal = SimpleNamespace(tenant_id=uuid4(), principal_id=uuid4())
    workflow_run_id = uuid4()
    calls = []

    class _Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, *_args, **_kwargs):
            existing = {
                "id": workflow_run_id,
                "project_id": identity["project_id"],
                "requested_by_principal_id": principal.principal_id,
                "kind": "mcad.agent.v2.generate",
                "request_payload_hash": submission_api.canonical_sha256(
                    submission_api.mcad_agent_v2_request_payload(
                        branch_id=identity["branch_id"],
                        expected_base_revision_id=identity[
                            "expected_base_revision_id"
                        ],
                        operation="generate",
                        objective="创建支架",
                        existing_code=None,
                        manufacturing_profile=None,
                            output_formats=("step", "stl"),
                            confirmation_timeout_seconds=3600,
                            modeling_backend="freecad",
                        )
                ),
            }
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(
                    one_or_none=lambda: existing
                )
            )

    async def start_v2(**kwargs):
        calls.append(kwargs)
        return workflow_run_id, object()

    monkeypatch.setattr(
        submission_api,
        "tenant_transaction",
        lambda *_args, **_kwargs: _Transaction(),
    )
    monkeypatch.setattr(
        submission_api, "start_mcad_agent_v2_workflow", start_v2
    )

    result = await submission_api.submit_durable_workflow(
        principal,
        **identity,
        operation="generate",
        objective="创建支架",
        output_formats=["step", "stl"],
    )

    assert result.workflow_run_id == workflow_run_id
    assert calls[0]["require_worker_ready"] is False


@pytest.mark.asyncio
async def test_async_generate_returns_persisted_workflow_id(monkeypatch):
    identity = _identity()
    workflow_run_id = uuid4()

    async def submit(principal, **kwargs):
        return SimpleNamespace(workflow_run_id=workflow_run_id)

    monkeypatch.setattr(batch_api.rate_limiter, "check", _no_rate_limit)
    monkeypatch.setattr(batch_api, "current_principal", lambda: object())
    monkeypatch.setattr(batch_api, "submit_durable_workflow", submit)

    response = await batch_api.generate_async(
        batch_api.AsyncGenerateRequest(
            prompt="创建支架",
            **identity,
        ),
        SimpleNamespace(),
        None,
    )

    assert response.task_id == str(workflow_run_id)
    assert response.status == "pending"


@pytest.mark.asyncio
async def test_cutover_restore_endpoint_rejects_direct_head_writes(monkeypatch):
    calls = []

    async def snapshot_belongs_to_user(snapshot_id, user_id):
        calls.append(("belongs", snapshot_id, user_id))
        return True

    monkeypatch.setattr(history_api, "snapshot_belongs_to_user", snapshot_belongs_to_user)
    with pytest.raises(HTTPException) as caught:
        await history_api.api_restore_model_snapshot("legacy-snapshot", user=None)
    assert caught.value.status_code == 410
    assert calls == []


@pytest.mark.asyncio
async def test_legacy_resume_is_read_only_input_to_durable_execution(
    monkeypatch,
):
    async def get_run(run_id):
        return {
            "id": run_id,
            "session_id": "session-1",
            "panel_id": "panel-1",
            "user_prompt": "创建支架",
        }

    async def list_steps(run_id):
        return [
            {
                "step_type": "resume_available",
                "status": "blocked",
                "output": {
                    "next_step": "execute_cad_code",
                    "resume_input": {
                        "code": "result = box(2, 2, 2)",
                        "output_formats": ["step"],
                    },
                },
            }
        ]

    monkeypatch.setattr(websocket_api.run_store, "get_run", get_run)
    monkeypatch.setattr(websocket_api.run_store, "list_steps", list_steps)
    code, formats, objective = await websocket_api._legacy_resume_input(
        "legacy-run",
        session_id="session-1",
        panel_id="panel-1",
    )
    assert code == "result = box(2, 2, 2)"
    assert formats == ["step"]
    assert objective == "创建支架"


@pytest.mark.asyncio
async def test_terminal_change_set_review_does_not_signal_closed_workflow(
    monkeypatch,
):
    async def snapshot(principal, workflow_run_id):
        return {"status": "succeeded"}

    async def unexpected_signal(*args, **kwargs):
        raise AssertionError("terminal Temporal workflow must not be signalled")

    monkeypatch.setattr(changes_api, "get_task_snapshot", snapshot)
    monkeypatch.setattr(
        changes_api,
        "confirm_mcad_workflow",
        unexpected_signal,
    )
    await changes_api._signal_review(
        object(),
        uuid4(),
        accepted=True,
        note="已审查",
    )
