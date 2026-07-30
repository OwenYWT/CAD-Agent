"""Real workspace/history flow through PostgreSQL revisions."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import settings
from app.db import close_database, tenant_transaction
from app.domain.identity import user_principal
from app.principal_context import bind_principal
from app.repositories.identity import reconcile_principal
from app.storage import history


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
    original = {
        "database_url": settings.database_url,
        "durable": settings.durable_control_plane_enabled,
        "history": settings.history_db_path,
    }
    settings.database_url = TEST_DATABASE_URL
    settings.durable_control_plane_enabled = True
    settings.history_db_path = "/path-that-must-not-be-created/history.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield
    settings.database_url = original["database_url"]
    settings.durable_control_plane_enabled = original["durable"]
    settings.history_db_path = original["history"]


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def database_engine_lifecycle(migrated_database):
    yield
    await close_database()


@pytest.mark.asyncio(loop_scope="module")
async def test_workspace_revisions_restore_feedback_and_tenant_isolation():
    suffix = uuid4().hex
    user_id = f"history-user-{suffix}"
    session_id = f"session-{suffix}"
    panel_id = f"panel-{suffix}"
    request_id = f"request-{suffix}"
    context = await reconcile_principal(user_principal(user_id))
    bind_principal(context)

    session = await history.create_session(session_id, "齿轮箱", user_id)
    assert session["title"] == "齿轮箱"
    panel = await history.create_panel(
        session_id,
        panel_id,
        "机械设计",
        user_id,
    )
    assert panel["session_id"] == session_id
    await history.save_message(panel_id, "user", "创建齿轮箱")
    assert await history.get_messages(panel_id) == [
        {"role": "user", "content": "创建齿轮箱"}
    ]

    first = await history.create_model_snapshot(
        panel_id,
        {
            "request_id": request_id,
            "success": True,
            "code": "result = box(10, 20, 30)",
            "files": {"step": f"/api/files/{request_id}/result.step"},
            "params": {"width": 10},
            "inspect_report": {
                "verdict": "pass",
                "available_exports": ["step"],
            },
        },
        "generate",
        "创建齿轮箱",
    )
    second = await history.create_model_snapshot(
        panel_id,
        {
            "request_id": request_id,
            "success": True,
            "code": "result = box(12, 20, 30)",
            "files": {"step": f"/api/files/{request_id}/result.step"},
            "params": {"width": 12},
        },
        "modify",
        "宽度改为 12mm",
        first["id"],
    )
    assert second["version"] == 2
    assert second["parent_snapshot_id"] == first["id"]
    assert [item["version"] for item in await history.list_model_snapshots(panel_id)] == [2, 1]

    restored = await history.restore_model_snapshot(first["id"], user_id)
    assert restored and restored["version"] == 1
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        kinds = (
            await connection.execute(
                text(
                    """
                    SELECT kind FROM project_revisions
                    WHERE tenant_id=:tenant
                      AND manifest->>'panel_id'=:panel
                    ORDER BY revision_number
                    """
                ),
                {"tenant": context.tenant_id, "panel": panel_id},
            )
        ).scalars().all()
    assert kinds == ["initial", "candidate", "rollback"]

    feedback = await history.save_feedback(
        request_id,
        rating="up",
        printed="yes",
        note="尺寸正确",
    )
    assert feedback["rating"] == "up"
    assert len(await history.get_feedback(request_id)) == 1

    link = await history.save_onshape_link(
        request_id,
        user_id,
        "document",
        "workspace",
        None,
        "translation",
        "processing",
        "https://cad.onshape.com/documents/document",
    )
    updated = await history.update_onshape_link_status(
        link["id"],
        "done",
        "element",
        "https://cad.onshape.com/documents/document/e/element",
    )
    assert updated and updated["status"] == "done"

    intruder = await reconcile_principal(
        user_principal(f"intruder-{suffix}")
    )
    bind_principal(intruder)
    assert await history.list_sessions(f"intruder-{suffix}") == []
    assert not await history.panel_belongs_to_user(
        panel_id,
        f"intruder-{suffix}",
    )
    assert await history.get_model_snapshot(first["id"]) is None

    bind_principal(context)
    await history.delete_session(session_id, user_id)
    assert await history.list_sessions(user_id) == []
