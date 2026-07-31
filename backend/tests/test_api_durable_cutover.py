from types import SimpleNamespace
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
from fastapi import HTTPException


def _identity():
    return {
        "project_id": uuid4(),
        "branch_id": uuid4(),
        "expected_base_revision_id": uuid4(),
        "idempotency_key": f"cutover-{uuid4()}",
    }


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

    monkeypatch.setattr(settings, "durable_api_cutover_enabled", True)
    monkeypatch.setattr(generate_api.rate_limiter, "check", _no_rate_limit)
    monkeypatch.setattr(generate_api, "current_principal", lambda: object())
    monkeypatch.setattr(generate_api, "submit_durable_workflow", submit)
    monkeypatch.setattr(
        generate_api,
        "wait_for_compatibility_response",
        wait,
    )
    monkeypatch.setattr(
        generate_api,
        "_get_orchestrator",
        lambda: (_ for _ in ()).throw(
            AssertionError("legacy orchestrator must not be called")
        ),
    )

    response = await generate_api.generate(
        GenerateRequest(prompt="创建支架", **identity),
        SimpleNamespace(),
        None,
    )

    assert response.workflow_run_id == workflow_run_id
    assert calls == [
        {
            **identity,
            "operation": "generate",
            "objective": "创建支架",
            "output_formats": ["step", "stl"],
            "manufacturing_profile": None,
        }
    ]


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

    monkeypatch.setattr(settings, "durable_api_cutover_enabled", True)
    monkeypatch.setattr(execute_api.rate_limiter, "check", _no_rate_limit)
    monkeypatch.setattr(execute_api, "current_principal", lambda: object())
    monkeypatch.setattr(execute_api, "submit_durable_workflow", submit)
    monkeypatch.setattr(
        execute_api,
        "wait_for_compatibility_response",
        wait,
    )
    monkeypatch.setattr(
        execute_api,
        "_get_orchestrator",
        lambda: (_ for _ in ()).throw(
            AssertionError("legacy executor must not be called")
        ),
    )

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
async def test_async_generate_returns_persisted_workflow_id(monkeypatch):
    identity = _identity()
    workflow_run_id = uuid4()

    async def submit(principal, **kwargs):
        return SimpleNamespace(workflow_run_id=workflow_run_id)

    monkeypatch.setattr(settings, "durable_api_cutover_enabled", True)
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
async def test_cutover_disables_legacy_snapshot_restore_writer(monkeypatch):
    monkeypatch.setattr(settings, "durable_api_cutover_enabled", True)
    with pytest.raises(HTTPException) as error:
        await history_api.api_restore_model_snapshot(
            "legacy-snapshot",
            user=None,
        )
    assert error.value.status_code == 410


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
