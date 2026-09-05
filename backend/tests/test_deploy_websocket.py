"""Hermetic contracts for the public Durable WebSocket submission surface."""
from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.agent import run_store
from app.api import websocket as ws_api
from app.api.auth import rate_limiter
from app.config import settings
from app.main import app
from app.services.durable_submission import WorkspaceIdentity
from app.services.operation_resolution import (
    RevisionSourceInventory,
    TrustedBaseSource,
)
from app.storage import history


def _identity() -> dict[str, str]:
    return {
        "project_id": str(uuid4()),
        "branch_id": str(uuid4()),
        "expected_base_revision_id": str(uuid4()),
        "idempotency_key": f"ws-{uuid4()}",
    }


@pytest.fixture
def durable_ws(tmp_path, monkeypatch):
    """Patch only infrastructure boundaries; the real WS route handles messages."""
    monkeypatch.setattr(settings, "durable_control_plane_enabled", True)
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    monkeypatch.setattr(settings, "api_keys", [])
    monkeypatch.setattr(rate_limiter, "rpm", 0)
    asyncio.run(history.close_db())
    ws_api.sessions.clear()
    calls: dict[str, list] = {
        "workspace": [],
        "submit": [],
        "cancel": [],
    }
    panel_sessions: dict[str, str] = {}
    workspace = WorkspaceIdentity(
        project_id=uuid4(),
        branch_id=uuid4(),
        head_revision_id=uuid4(),
    )

    async def ensure(principal, **kwargs):
        calls["workspace"].append((principal, kwargs))
        return workspace

    async def reconcile(principal):
        return principal

    async def session_writable_by_user(_session_id, _user_id):
        return True

    async def panel_writable_by_session(panel_id, session_id, _user_id):
        owner_session_id = panel_sessions.get(panel_id)
        return owner_session_id is None or owner_session_id == session_id

    async def submit(principal, **kwargs):
        workflow_run_id = uuid4()
        calls["submit"].append((principal, kwargs, workflow_run_id))
        return SimpleNamespace(workflow_run_id=workflow_run_id)

    async def cancel(**kwargs):
        calls["cancel"].append(kwargs)

    inventory = {"value": RevisionSourceInventory()}

    async def load_inventory(_principal, **_kwargs):
        return inventory["value"]

    monkeypatch.setattr(ws_api, "ensure_workspace_identity", ensure)
    from app.repositories import identity as identity_repository

    monkeypatch.setattr(identity_repository, "reconcile_principal", reconcile)
    monkeypatch.setattr(
        history,
        "session_writable_by_user",
        session_writable_by_user,
    )
    monkeypatch.setattr(
        history,
        "panel_writable_by_session",
        panel_writable_by_session,
    )
    monkeypatch.setattr(ws_api, "submit_durable_workflow", submit)
    monkeypatch.setattr(ws_api, "cancel_mcad_workflow", cancel)
    monkeypatch.setattr(
        ws_api,
        "load_revision_source_inventory",
        load_inventory,
    )
    yield SimpleNamespace(
        calls=calls,
        workspace=workspace,
        inventory=inventory,
        panel_sessions=panel_sessions,
    )
    asyncio.run(history.close_db())
    ws_api.sessions.clear()
    rate_limiter._windows.clear()


@pytest.fixture
def client():
    return TestClient(app)


def test_websocket_fails_closed_when_durable_runtime_is_disabled(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "durable_control_plane_enabled", False)

    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/ws/session-durable-required") as socket:
            socket.receive_json()

    assert error.value.code == 1013


def test_first_prompt_creates_workspace_then_submits(client, durable_ws):
    with client.websocket_connect("/ws/session-first") as socket:
        socket.send_json({
            "type": "user_message",
            "panel_id": "panel-first",
            "text": "创建一个安装支架",
            "idempotency_key": "first-prompt-1",
        })
        message = socket.receive_json()

    assert message["type"] == "task_submitted"
    assert message["data"]["project_id"] == str(durable_ws.workspace.project_id)
    assert len(durable_ws.calls["workspace"]) == 1
    submitted = durable_ws.calls["submit"][0][1]
    assert submitted["operation"] == "generate"
    assert submitted["objective"] == "创建一个安装支架"
    assert submitted["idempotency_key"] == "first-prompt-1"


def test_dxf_prompt_keeps_durable_generate_path(client, durable_ws):
    identity = _identity()
    with client.websocket_connect("/ws/session-dxf") as socket:
        socket.send_json({
            "type": "user_message",
            "panel_id": "panel-dxf",
            "text": "绘制法兰轮廓",
            "capability": "dxf",
            **identity,
        })
        assert socket.receive_json()["type"] == "task_submitted"

    submitted = durable_ws.calls["submit"][0][1]
    assert submitted["operation"] == "generate"
    assert submitted["output_formats"] == ["dxf"]
    assert "二维 DXF" in submitted["objective"]


@pytest.mark.parametrize(
    ("payload", "operation"),
    [
        ({
            "type": "modify_part",
            "part_name": "支架",
            "instruction": "宽度增加 2 mm",
            "code": "result = box(10, 10, 10)",
        }, "modify"),
        ({
            "type": "execute_code",
            "code": "result = box(10, 10, 10)",
        }, "execute"),
    ],
)
def test_existing_project_writes_submit_durable(
    client, durable_ws, payload, operation
):
    if operation == "modify":
        code = payload["code"]
        durable_ws.inventory["value"] = RevisionSourceInventory(cadquery=(
            TrustedBaseSource(
                kind="revision_manifest_source",
                source_id=uuid4(),
                sha256=hashlib.sha256(code.encode("utf-8")).hexdigest(),
                code=code,
            ),
        ))
    with client.websocket_connect(f"/ws/session-{operation}") as socket:
        socket.send_json({"panel_id": f"panel-{operation}", **payload, **_identity()})
        message = socket.receive_json()

    assert message["type"] == "task_submitted"
    assert durable_ws.calls["submit"][0][1]["operation"] == operation


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "user_message", "text": "创建盒子"},
        {"type": "execute_code", "code": "result = box(1, 1, 1)"},
        {
            "type": "modify_part",
            "part_name": "盒子",
            "instruction": "加宽",
            "code": "result = box(1, 1, 1)",
        },
    ],
)
def test_write_identity_is_required_before_submission(
    client, durable_ws, payload
):
    with client.websocket_connect("/ws/session-invalid-identity") as socket:
        socket.send_json({"panel_id": "panel-invalid", **payload})
        message = socket.receive_json()

    assert message["type"] == "generation_result"
    assert message["data"]["error"]["type"] == "ValidationError"
    assert durable_ws.calls["submit"] == []


def test_invalid_modify_body_is_rejected(client, durable_ws):
    with client.websocket_connect("/ws/session-invalid-modify") as socket:
        socket.send_json({
            "type": "modify_part",
            "panel_id": "panel-invalid-modify",
            "part_name": "",
            "instruction": "加宽",
            "code": "result = box(1, 1, 1)",
            **_identity(),
        })
        message = socket.receive_json()

    assert message["data"]["error"]["type"] == "ValidationError"
    assert durable_ws.calls["submit"] == []


def test_submission_error_is_public_and_connection_survives(
    client, durable_ws, monkeypatch
):
    async def fail(*_args, **_kwargs):
        raise RuntimeError("durable backend unavailable")

    monkeypatch.setattr(ws_api, "submit_durable_workflow", fail)
    with client.websocket_connect("/ws/session-error") as socket:
        socket.send_json({
            "type": "execute_code",
            "panel_id": "panel-error",
            "code": "result = box(1, 1, 1)",
            **_identity(),
        })
        message = socket.receive_json()
        socket.send_json({"type": "unknown", "panel_id": "panel-error"})
        next_message = socket.receive_json()

    assert message["data"]["error"]["type"] == "RuntimeError"
    assert next_message["data"]["error"]["type"] == "ValidationError"


def test_cancel_targets_persisted_workflow(client, durable_ws):
    workflow_run_id = uuid4()
    with client.websocket_connect("/ws/session-cancel") as socket:
        socket.send_json({
            "type": "cancel",
            "panel_id": "panel-cancel",
            "workflow_run_id": str(workflow_run_id),
        })
        message = socket.receive_json()

    assert message["type"] == "task_status"
    assert message["data"]["status"] == "cancellation_requested"
    assert durable_ws.calls["cancel"][0]["workflow_run_id"] == workflow_run_id


def test_cancel_without_workflow_id_returns_not_found(client, durable_ws):
    with client.websocket_connect("/ws/session-no-cancel") as socket:
        socket.send_json({"type": "cancel", "panel_id": "panel-no-cancel"})
        message = socket.receive_json()

    assert message["data"]["error"]["type"] == "TaskNotFound"
    assert durable_ws.calls["cancel"] == []


def test_restore_task_declares_event_subscription(client, durable_ws):
    workflow_run_id = uuid4()
    with client.websocket_connect("/ws/session-restore") as socket:
        socket.send_json({
            "type": "restore_task",
            "panel_id": "panel-restore",
            "workflow_run_id": str(workflow_run_id),
        })
        message = socket.receive_json()

    assert message == {
        "type": "task_status",
        "data": {
            "task_id": str(workflow_run_id),
            "panel_id": "panel-restore",
            "status": "durable_subscription",
        },
    }


def test_restore_context_cannot_inject_product_state(client, durable_ws):
    with client.websocket_connect("/ws/session-context") as socket:
        socket.send_json({
            "type": "restore_context",
            "panel_id": "panel-context",
            "code": "malicious = host_state",
        })
        socket.send_json({"type": "unknown", "panel_id": "panel-context"})
        message = socket.receive_json()

    assert message["data"]["error"]["type"] == "ValidationError"
    assert "session-context" not in ws_api.sessions


def test_legacy_resume_is_resubmitted_as_durable_execute(
    client, durable_ws
):
    run = asyncio.run(
        run_store.create_run("session-resume", "继续模型", panel_id="panel-resume")
    )
    step = asyncio.run(
        run_store.start_step(
            run["id"], "resume_available", {"next_step": "execute_cad_code"}
        )
    )
    asyncio.run(run_store.complete_step(
        step["id"],
        {
            "next_step": "execute_cad_code",
            "resume_input": {
                "code": "result = box(2, 2, 2)",
                "output_formats": ["step"],
                "user_prompt": "继续模型",
            },
        },
        status="blocked",
    ))

    with client.websocket_connect("/ws/session-resume") as socket:
        socket.send_json({
            "type": "resume_run",
            "run_id": run["id"],
            "panel_id": "panel-resume",
            **_identity(),
        })
        message = socket.receive_json()

    assert message["type"] == "task_submitted"
    submitted = durable_ws.calls["submit"][0][1]
    assert submitted["operation"] == "execute"
    assert submitted["code"] == "result = box(2, 2, 2)"


def test_disconnect_never_cancels_durable_workflow(client, durable_ws):
    with client.websocket_connect("/ws/session-disconnect") as socket:
        socket.send_json({
            "type": "execute_code",
            "panel_id": "panel-disconnect",
            "code": "result = box(1, 1, 1)",
            **_identity(),
        })
        assert socket.receive_json()["type"] == "task_submitted"

    assert len(durable_ws.calls["submit"]) == 1
    assert durable_ws.calls["cancel"] == []


def test_panel_cannot_be_reused_across_sessions(client, durable_ws):
    durable_ws.panel_sessions["shared-panel"] = "owner-session"

    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/ws/other-session") as socket:
            socket.send_json({
                "type": "restore_task",
                "panel_id": "shared-panel",
                "workflow_run_id": str(uuid4()),
            })
            socket.receive_json()
    assert error.value.code == 4003


def test_invalid_session_id_closes_4001(client, durable_ws):
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/ws/bad session!") as socket:
            socket.receive_json()
    assert error.value.code == 4001
