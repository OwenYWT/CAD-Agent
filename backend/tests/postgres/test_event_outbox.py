"""Real PostgreSQL event sequence and transactional-outbox tests."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import settings
from app.db import close_database, get_database_engine, tenant_transaction
from app.domain.identity import user_principal
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.repositories.runs import append_workflow_event
from app.services.run_state import create_workflow


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
ORIGINAL_DATABASE_URL = settings.database_url
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="CAD_AGENT_TEST_DATABASE_URL is required for real PostgreSQL tests",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    if not TEST_DATABASE_URL:
        yield
        return
    settings.database_url = TEST_DATABASE_URL
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def database_engine_lifecycle(migrated_database):
    settings.database_url = TEST_DATABASE_URL
    yield
    await close_database()
    settings.database_url = ORIGINAL_DATABASE_URL


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean_control_plane():
    if not TEST_DATABASE_URL:
        yield
        return
    async with get_database_engine().begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE outbox_messages, task_events, execution_attempts, "
                "step_runs, workflow_runs, usage_meter_entries, audit_records, "
                "project_memberships, projects, tenant_memberships, principals, "
                "tenants CASCADE"
            )
        )
    yield


async def _seed_workflow():
    owner = user_principal(f"event-owner-{uuid4()}")
    project_id = uuid4()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        await ensure_principal(connection, owner)
        await create_project(
            connection,
            project_id=project_id,
            tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id,
            name="Event project",
            slug=f"events-{project_id.hex[:8]}",
        )
        workflow = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="generate",
            idempotency_key=f"event-workflow-{project_id}",
            request_payload={"prompt": "事件测试"},
        )
    return owner, workflow.workflow_id


@pytest.mark.asyncio(loop_scope="module")
async def test_event_and_outbox_rows_commit_together_with_matching_payloads():
    owner, workflow_id = await _seed_workflow()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        event = await append_workflow_event(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow_id,
            event_type="progress",
            payload={
                "progress": 25,
                "message": "建模中",
                "authorization": "Bearer must-not-be-stored",
            },
        )
        row = (
            await connection.execute(
                text(
                    "SELECT e.sequence, e.event_type, e.payload AS event_payload, "
                    "o.payload AS outbox_payload, o.topic "
                    "FROM task_events e JOIN outbox_messages o ON o.event_id=e.id "
                    "WHERE e.id=:event_id"
                ),
                {"event_id": event.event_id},
            )
        ).mappings().one()

    assert row["sequence"] == event.sequence
    assert row["event_type"] == "progress"
    assert row["event_payload"] == {
        "progress": 25,
        "message": "建模中",
        "authorization": "[REDACTED]",
    }
    assert row["outbox_payload"]["sequence"] == event.sequence
    assert row["outbox_payload"]["payload"]["authorization"] == "[REDACTED]"
    assert "must-not-be-stored" not in str(row)
    assert row["topic"] == "workflow.events"


@pytest.mark.asyncio(loop_scope="module")
async def test_every_run_table_forces_tenant_rls():
    expected_tables = {
        "workflow_runs",
        "step_runs",
        "execution_attempts",
        "task_events",
        "outbox_messages",
    }
    async with get_database_engine().connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:table_names)"
                ),
                {"table_names": list(expected_tables)},
            )
        ).mappings().all()

    assert {row["relname"] for row in rows} == expected_tables
    assert all(row["relrowsecurity"] for row in rows)
    assert all(row["relforcerowsecurity"] for row in rows)


@pytest.mark.asyncio(loop_scope="module")
async def test_concurrent_events_get_one_gapless_monotonic_workflow_sequence():
    owner, workflow_id = await _seed_workflow()

    async def append(index: int):
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            return await append_workflow_event(
                connection,
                tenant_id=owner.tenant_id,
                workflow_id=workflow_id,
                event_type="progress",
                payload={"index": index},
            )

    events = await asyncio.gather(*(append(index) for index in range(30)))
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        sequences = list(
            (
                await connection.execute(
                    text(
                        "SELECT sequence FROM task_events "
                        "WHERE workflow_run_id=:workflow_id ORDER BY sequence"
                    ),
                    {"workflow_id": workflow_id},
                )
            ).scalars()
        )
        event_count = await connection.scalar(
            text(
                "SELECT count(*) FROM task_events "
                "WHERE workflow_run_id=:workflow_id"
            ),
            {"workflow_id": workflow_id},
        )
        outbox_count = await connection.scalar(
            text(
                "SELECT count(*) FROM outbox_messages "
                "WHERE workflow_run_id=:workflow_id"
            ),
            {"workflow_id": workflow_id},
        )

    # Sequence 1 is workflow.created; concurrent progress events follow it.
    assert sequences == list(range(1, 32))
    assert sorted(event.sequence for event in events) == list(range(2, 32))
    assert event_count == outbox_count == 31


@pytest.mark.asyncio(loop_scope="module")
async def test_transaction_rollback_leaves_neither_event_nor_outbox():
    owner, workflow_id = await _seed_workflow()
    with pytest.raises(RuntimeError, match="force rollback"):
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            await append_workflow_event(
                connection,
                tenant_id=owner.tenant_id,
                workflow_id=workflow_id,
                event_type="progress",
                payload={"progress": 50},
            )
            raise RuntimeError("force rollback")

    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        event_count = await connection.scalar(
            text(
                "SELECT count(*) FROM task_events "
                "WHERE workflow_run_id=:workflow_id"
            ),
            {"workflow_id": workflow_id},
        )
        outbox_count = await connection.scalar(
            text(
                "SELECT count(*) FROM outbox_messages "
                "WHERE workflow_run_id=:workflow_id"
            ),
            {"workflow_id": workflow_id},
        )
        last_sequence = await connection.scalar(
            text("SELECT last_event_sequence FROM workflow_runs WHERE id=:id"),
            {"id": workflow_id},
        )

    assert event_count == outbox_count == 1
    assert last_sequence == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_task_events_are_append_only():
    owner, workflow_id = await _seed_workflow()
    with pytest.raises(Exception, match="append-only|permission denied"):
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            await connection.execute(
                text(
                    "UPDATE task_events SET event_type='tampered' "
                    "WHERE workflow_run_id=:workflow_id"
                ),
                {"workflow_id": workflow_id},
            )
