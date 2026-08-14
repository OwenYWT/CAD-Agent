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
async def test_snapshot_automatically_links_to_latest_panel_revision():
    await history.create_session("session-lineage", user_id="user-1")
    await history.create_panel(
        "session-lineage",
        "panel-lineage",
        user_id="user-1",
    )
    first = await history.create_model_snapshot(
        "panel-lineage",
        {
            "success": True,
            "request_id": "req-first",
            "code": "width = 20",
        },
        source="generation",
    )
    second = await history.create_model_snapshot(
        "panel-lineage",
        {
            "success": True,
            "request_id": "req-second",
            "code": "width = 24",
        },
        source="execute_code",
    )

    assert first["parent_snapshot_id"] is None
    assert second["parent_snapshot_id"] == first["id"]

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
    messages = await history.get_messages("panel-1")
    assert restored["code"] == "result = restored"
    assert restored["result"]["code"] == "result = restored"
    assert restored["result"]["snapshot_id"] == snapshot["id"]
    assert restored["result"]["version"] == 1
    assert restored["status"] == "warn"
    assert panels[0]["current_code"] == "result = restored"
    assert messages[-1]["content"] == "已恢复模型版本 v1"
    assert messages[-1]["result"]["snapshot_id"] == snapshot["id"]
    assert messages[-1]["result"]["code"] == "result = restored"


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
    assert restored.status_code == 410
    assert "持久化 MCAD" in restored.json()["detail"]


@pytest.mark.asyncio
async def test_snapshot_preserves_assembly_parts_for_durable_read():
    await history.create_session("session-assembly", user_id="user-1")
    await history.create_panel(
        "session-assembly",
        "panel-assembly",
        user_id="user-1",
    )
    snapshot = await history.create_model_snapshot(
        "panel-assembly",
        {
            "success": True,
            "request_id": "req-assembly",
            "code": "result = assembly",
            "assembly_parts": [
                {
                    "part_id": "base",
                    "name": "base",
                    "code": "result = base",
                    "code_hash": "hash-base",
                },
                {
                    "part_id": "lid",
                    "name": "lid",
                    "code": "result = lid",
                    "code_hash": "hash-lid",
                },
            ],
        },
        source="generation",
        prompt="make assembly",
    )

    stored = await history.get_model_snapshot(snapshot["id"])

    assert stored["result"]["assembly_parts"][0]["part_id"] == "base"
    assert stored["result"]["assembly_parts"][0]["code_hash"] == "hash-base"
    assert stored["result"]["assembly_parts"][1]["part_id"] == "lid"
    assert stored["result"]["assembly_parts"][1]["code_hash"] == "hash-lid"


def test_snapshot_api_diff_prioritizes_file_and_part_changes(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    asyncio.run(history.close_db())
    asyncio.run(history.create_session("session-diff", user_id=None))
    asyncio.run(history.create_panel("session-diff", "panel-diff", user_id=None))
    before = asyncio.run(history.create_model_snapshot(
        "panel-diff",
        {
            "success": True,
            "request_id": "req-before",
            "code": "result = before",
            "files": {"step": "/api/files/before/result.step"},
            "params": {"width": {"value": 10, "comment": "mm"}},
            "assembly_parts": [
                {"part_id": "base", "name": "base", "code_hash": "hash-base"},
                {"part_id": "lid", "name": "lid", "code_hash": "hash-lid"},
            ],
            "inspect_report": {
                "verdict": "pass",
                "bounding_box": {"x_min": 0, "x_max": 10},
            },
        },
        source="generation",
    ))
    after = asyncio.run(history.create_model_snapshot(
        "panel-diff",
        {
            "success": True,
            "request_id": "req-after",
            "code": "result = after",
            "files": {
                "step": "/api/files/after/result.step",
                "stl": "/api/files/after/result.stl",
            },
            "params": {"width": {"value": 12, "comment": "mm"}},
            "assembly_parts": [
                {"part_id": "base", "name": "base", "code_hash": "hash-base"},
                {"part_id": "lid", "name": "lid", "code_hash": "hash-lid-2"},
            ],
            "inspect_report": {
                "verdict": "warn",
                "bounding_box": {"x_min": 0, "x_max": 12},
            },
        },
        source="modify_part",
        parent_snapshot_id=before["id"],
    ))
    client = TestClient(app)

    response = client.get(
        f"/api/history/snapshots/{before['id']}/diff/{after['id']}"
    )

    assert response.status_code == 200
    diff = response.json()
    assert diff["from_snapshot_id"] == before["id"]
    assert diff["to_snapshot_id"] == after["id"]
    assert diff["file_changes"]["changed"] == ["step"]
    assert diff["file_changes"]["added"] == ["stl"]
    assert diff["part_changes"]["changed"] == ["lid"]
    assert diff["part_changes"]["unchanged"] == ["base"]
    assert diff["parameter_changes"]["changed"] == ["width"]
    assert diff["model_changes"]["inspect_verdict"] == {
        "from": "pass",
        "to": "warn",
    }
