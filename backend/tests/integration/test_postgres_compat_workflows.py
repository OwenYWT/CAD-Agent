"""Real async compatibility task lifecycle in workflow_runs/task_events."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config

from app.config import settings
from app.db import close_database
from app.storage import local_runs


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="CAD_AGENT_TEST_DATABASE_URL is required",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    if not TEST_DATABASE_URL:
        yield
        return
    original = (
        settings.database_url,
        settings.durable_control_plane_enabled,
        settings.history_db_path,
    )
    settings.database_url = TEST_DATABASE_URL
    settings.durable_control_plane_enabled = True
    settings.history_db_path = "/path-that-must-not-be-created/history.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield
    (
        settings.database_url,
        settings.durable_control_plane_enabled,
        settings.history_db_path,
    ) = original


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def database_engine_lifecycle(migrated_database):
    yield
    await close_database()


@pytest.mark.asyncio(loop_scope="module")
async def test_compat_workflow_progress_result_cancel_and_owner_isolation():
    suffix = uuid4().hex
    owner = f"user:workflow-{suffix}"
    scope = f"direct:{suffix}"
    run_id = await local_runs.create_run(
        kind="generate",
        owner=owner,
        request={"prompt": "创建测试零件"},
        scope_key=scope,
    )
    pending = await local_runs.get_run(run_id, owner)
    assert pending and pending["state"] == "PENDING"
    await local_runs.mark_running(run_id)
    await local_runs.append_progress(run_id, {"stage": "modeling"})
    result = {
        "request_id": f"request-{suffix}",
        "success": True,
        "files": {"step": "/api/files/request/result.step"},
    }
    assert await local_runs.commit_result(run_id, result) == result
    completed = await local_runs.get_run(run_id, owner)
    assert completed and completed["state"] == "COMPLETED"
    assert completed["result"] == result
    assert (
        await local_runs.find_latest_for_scope(scope, owner)
    )["id"] == run_id
    events = await local_runs.list_events(run_id)
    assert [event["event_type"] for event in events].count("progress") == 1
    assert events[-1]["state"] is None

    cancel_id = await local_runs.create_run(
        kind="generate",
        owner=owner,
        request={"prompt": "取消任务"},
    )
    assert await local_runs.request_cancel(cancel_id, owner)
    cancelled = await local_runs.get_run(cancel_id, owner)
    assert cancelled and cancelled["state"] == "CANCEL_REQUESTED"
    assert cancelled["cancel_requested"]

    assert await local_runs.get_run(run_id, f"user:intruder-{suffix}") is None
    assert await local_runs.reconcile_incomplete_runs() == []
