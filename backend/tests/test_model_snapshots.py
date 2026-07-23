import asyncio

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models.schemas import GenerateResponse, GenerationResult
from app.storage import history


@pytest_asyncio.fixture(autouse=True)
async def isolated_history_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    await history.close_db()
    yield
    await history.close_db()


def test_generate_response_serializes_snapshot_metadata():
    response = GenerateResponse(
        request_id="req-1",
        success=True,
        snapshot_id="snap-1",
        version=3,
    )
    dumped = response.model_dump()
    assert dumped["snapshot_id"] == "snap-1"
    assert dumped["version"] == 3


def test_generation_result_serializes_snapshot_metadata():
    result = GenerationResult(
        request_id="req-1",
        success=True,
        snapshot_id="snap-1",
        version=3,
    )
    dumped = result.model_dump()
    assert dumped["snapshot_id"] == "snap-1"
    assert dumped["version"] == 3

@pytest.mark.asyncio
async def test_create_and_list_model_snapshots_versions_per_panel():
    await history.create_session("session-1", user_id="user-1")
    await history.create_panel("session-1", "panel-1", user_id="user-1")
    result = {
        "success": True,
        "request_id": "req-1",
        "code": "result = None",
        "files": {"stl": "/api/files/req-1/result.stl"},
        "inspect_report": {"verdict": "pass", "available_exports": ["stl"]},
    }
    first = await history.create_model_snapshot("panel-1", result, source="generation", prompt="make a box")
    second = await history.create_model_snapshot(
        "panel-1",
        result,
        source="execute_code",
        prompt="parameter edit",
        parent_snapshot_id=first["id"],
    )
    snapshots = await history.list_model_snapshots("panel-1")
    assert first["version"] == 1
    assert second["version"] == 2
    assert second["parent_snapshot_id"] == first["id"]
    assert [s["version"] for s in snapshots] == [2, 1]
    assert snapshots[0]["status"] == "pass"
    assert snapshots[0]["available_exports"] == ["stl"]

@pytest.mark.asyncio
async def test_concurrent_snapshots_get_unique_monotonic_versions():
    await history.create_session("session-concurrent", user_id="user-1")
    await history.create_panel("session-concurrent", "panel-concurrent", user_id="user-1")
    result = {"success": True, "request_id": "req", "code": "result = None"}

    snapshots = await asyncio.gather(*(
        history.create_model_snapshot(
            "panel-concurrent",
            {**result, "request_id": f"req-{index}"},
            source="execute_code",
        )
        for index in range(10)
    ))

    assert sorted(snapshot["version"] for snapshot in snapshots) == list(range(1, 11))


@pytest.mark.asyncio
async def test_restore_model_snapshot_updates_panel_current_code():
    await history.create_session("session-1", user_id="user-1")
    await history.create_panel("session-1", "panel-1", user_id="user-1")
    snapshot = await history.create_model_snapshot(
        "panel-1",
        {
            "success": True,
            "request_id": "req-1",
            "code": "result = restored",
            "params": {"width": {"value": 10, "comment": "mm"}},
            "inspect_report": {"verdict": "warn"},
        },
        source="generation",
        prompt="make restored model",
    )
    restored = await history.restore_model_snapshot(snapshot["id"], user_id="user-1")
    panels = await history.list_panels("session-1")
    assert restored["code"] == "result = restored"
    assert restored["result"]["code"] == "result = restored"
    assert restored["status"] == "warn"
    assert panels[0]["current_code"] == "result = restored"


@pytest.mark.asyncio
async def test_delete_session_cascades_model_snapshots():
    await history.create_session("session-1", user_id="user-1")
    await history.create_panel("session-1", "panel-1", user_id="user-1")
    snapshot = await history.create_model_snapshot(
        "panel-1",
        {"success": True, "request_id": "req-1", "code": "result = x"},
        source="generation",
    )
    await history.delete_session("session-1", user_id="user-1")
    assert await history.get_model_snapshot(snapshot["id"]) is None

def test_snapshot_api_list_detail_and_restore(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    asyncio.run(history.close_db())
    asyncio.run(history.create_session("session-1", user_id=None))
    asyncio.run(history.create_panel("session-1", "panel-1", user_id=None))
    snapshot = asyncio.run(history.create_model_snapshot(
        "panel-1",
        {
            "success": True,
            "request_id": "req-1",
            "code": "result = api",
            "files": {"stl": "/api/files/req-1/result.stl"},
            "inspect_report": {"verdict": "pass", "available_exports": ["stl"]},
        },
        source="generation",
        prompt="make api model",
    ))
    client = TestClient(app)
    listed = client.get("/api/history/panels/panel-1/snapshots")
    detail = client.get(f"/api/history/snapshots/{snapshot['id']}")
    restored = client.post(f"/api/history/snapshots/{snapshot['id']}/restore")
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == snapshot["id"]
    assert listed.json()[0]["available_exports"] == ["stl"]
    assert detail.status_code == 200
    assert detail.json()["result"]["code"] == "result = api"
    assert restored.status_code == 200
    assert restored.json()["code"] == "result = api"
