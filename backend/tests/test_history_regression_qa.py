"""Regression coverage for issues found by the 2026-07-16 full-stack QA run."""

import asyncio

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import app.api.websocket as ws_api
from app.config import settings
from app.main import app
from app.models.schemas import GenerateResponse, ValidationResult
from app.capabilities import registry
from app.storage import history
from app.storage.file_ownership import claim_request_owner


@pytest_asyncio.fixture
async def isolated_history(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    await history.close_db()
    yield
    await history.close_db()


@pytest.mark.asyncio
async def test_first_real_prompt_promotes_placeholder_session_title(isolated_history):
    """A panel may create the session before the first prompt supplies its title."""
    await history.create_session("qa-session", title="", user_id="user-1")
    await history.create_session(
        "qa-session",
        title="生成一个 16 x 12 x 8 mm 的实心长方体",
        user_id="user-1",
    )

    sessions = await history.list_sessions("user-1")

    assert sessions == [
        {
            "id": "qa-session",
            "title": "生成一个 16 x 12 x 8 mm 的实心长方体",
            "created_at": sessions[0]["created_at"],
            "updated_at": sessions[0]["updated_at"],
        }
    ]


@pytest.mark.asyncio
async def test_existing_session_title_is_not_replaced(isolated_history):
    await history.create_session("qa-session", title="原始项目名称", user_id="user-1")

    session = await history.create_session(
        "qa-session",
        title="后续聊天内容不应替换项目名称",
        user_id="user-1",
    )

    assert session["title"] == "原始项目名称"


class _ExecuteOrchestrator:
    async def execute_code(self, code, output_formats=None):
        return GenerateResponse(
            request_id="qa-execute",
            success=True,
            files={"stl": "/api/files/qa-execute/result.stl"},
            code=code,
            validation=ValidationResult(is_watertight=True, volume=125.0),
        )


@pytest.fixture
def isolated_websocket_history(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    monkeypatch.setattr(settings, "api_keys", [])
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)
    monkeypatch.setattr(ws_api.rate_limiter, "rpm", 0)
    monkeypatch.setattr(ws_api, "_orchestrator", _ExecuteOrchestrator())
    asyncio.run(history.close_db())
    ws_api.sessions.clear()
    yield
    asyncio.run(history.close_db())
    ws_api.sessions.clear()


def test_execute_code_persists_complete_result(isolated_websocket_history):
    code = "result = box(5, 5, 5)\nshow_object(result)"

    with TestClient(app).websocket_connect("/ws/qa-session") as websocket:
        websocket.send_json(
            {"type": "execute_code", "code": code, "panel_id": "qa-panel"}
        )
        for _ in range(4):
            message = websocket.receive_json()
            if message["type"] == "generation_result":
                break
        else:
            raise AssertionError("WebSocket did not emit generation_result")

    messages = asyncio.run(history.get_messages("qa-panel"))
    panels = asyncio.run(history.list_panels("qa-session"))

    assert message["data"]["validation"]["is_watertight"] is True
    assert panels[0]["current_code"] == code
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["result"] == message["data"]


@pytest.mark.auth
def test_generated_files_require_authentication(tmp_path, monkeypatch):
    storage = tmp_path / "files"
    request_dir = storage / "qa-file"
    request_dir.mkdir(parents=True)
    (request_dir / "result.stl").write_bytes(b"solid qa\nendsolid qa\n")
    monkeypatch.setattr(settings, "file_storage_dir", str(storage))
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "auth_token_secret", "test-only-file-auth-secret-with-sufficient-length-0001")
    monkeypatch.setattr(settings, "api_keys", ["qa-static-key"])
    claim_request_owner("qa-file", "qa-static-key")

    with TestClient(app) as client:
        unauthenticated = client.get("/api/files/qa-file/result.stl")
        authenticated = client.get(
            "/api/files/qa-file/result.stl",
            headers={"Authorization": "Bearer qa-static-key"},
        )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.content == b"solid qa\nendsolid qa\n"


@pytest.mark.auth
def test_generated_files_are_isolated_between_authenticated_users(tmp_path, monkeypatch):
    storage = tmp_path / "files"
    request_dir = storage / "qa-owned-file"
    request_dir.mkdir(parents=True)
    (request_dir / "result.stl").write_bytes(b"solid private\nendsolid private\n")
    monkeypatch.setattr(settings, "file_storage_dir", str(storage))
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "auth_token_secret", "test-only-file-auth-secret-with-sufficient-length-0001")
    monkeypatch.setattr(settings, "api_keys", ["owner-key", "other-key"])
    claim_request_owner("qa-owned-file", "owner-key")

    with TestClient(app) as client:
        owner = client.get(
            "/api/files/qa-owned-file/result.stl",
            headers={"Authorization": "Bearer owner-key"},
        )
        other = client.get(
            "/api/files/qa-owned-file/result.stl",
            headers={"Authorization": "Bearer other-key"},
        )

    assert owner.status_code == 200
    assert other.status_code == 404


def test_capability_actions_report_runtime_blockers(monkeypatch):
    monkeypatch.setattr(settings, "cadskills_isolated_executor", [])
    monkeypatch.setattr(registry, "_module_available", lambda name: name == "ezdxf")

    dxf = registry.get_capability("dxf")
    viewer = registry.get_capability("cad-viewer")

    assert dxf is not None and dxf.available is False
    assert all(action.available is False for action in dxf.actions)
    assert all(action.blocked_reason for action in dxf.actions)
    assert dxf.blocked_reasons
    assert viewer is not None
    assert all(action.available is True for action in viewer.actions)
