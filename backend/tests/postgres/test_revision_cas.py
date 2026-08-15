"""Real PostgreSQL tests for immutable revisions and branch-head CAS."""
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
from app.repositories.revisions import (
    StaleBaseRevision,
    compare_and_swap_branch_head,
    create_candidate_change_set,
    create_initial_branch,
)


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
                "TRUNCATE change_sets, project_revisions, project_branches, "
                "outbox_messages, task_events, execution_attempts, step_runs, "
                "workflow_runs, usage_meter_entries, audit_records, "
                "project_memberships, projects, tenant_memberships, principals, "
                "tenants CASCADE"
            )
        )
    yield


async def _seed_project():
    owner = user_principal(f"revision-owner-{uuid4()}")
    project_id = uuid4()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        await ensure_principal(connection, owner)
        await create_project(
            connection,
            project_id=project_id,
            tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id,
            name="Revision project",
            slug=f"revision-{project_id.hex[:8]}",
        )
    return owner, project_id


def _manifest(width: int) -> dict:
    return {
        "parameters": {"width": {"value": width, "unit": "mm"}},
        "geometry": {"generator": "cadquery", "code_hash": f"{width:064x}"},
    }


@pytest.mark.asyncio(loop_scope="module")
async def test_initial_branch_and_candidate_revision_are_immutable_and_uncommitted():
    owner, project_id = await _seed_project()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        initial = await create_initial_branch(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            created_by_principal_id=owner.principal_id,
            branch_name="main",
            initial_manifest=_manifest(20),
        )
        candidate = await create_candidate_change_set(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            created_by_principal_id=owner.principal_id,
            idempotency_key="change-width-24",
            objective="将宽度调整为 24 mm",
            candidate_manifest=_manifest(24),
            change_summary={"parameters": [{"name": "width", "before": 20, "after": 24}]},
            validation_summary={"status": "pending"},
            risk_summary={"level": "low"},
        )
        branch_head = await connection.scalar(
            text("SELECT head_revision_id FROM project_branches WHERE id=:id"),
            {"id": initial.branch_id},
        )
        revisions = (
            await connection.execute(
                text(
                    "SELECT id, parent_revision_id, revision_number "
                    "FROM project_revisions WHERE branch_id=:branch_id "
                    "ORDER BY revision_number"
                ),
                {"branch_id": initial.branch_id},
            )
        ).mappings().all()
        change_set_status = await connection.scalar(
            text("SELECT status FROM change_sets WHERE id=:id"),
            {"id": candidate.change_set_id},
        )

    assert initial.revision_number == 1
    assert candidate.revision_number == 2
    assert branch_head == initial.revision_id
    assert revisions == [
        {
            "id": initial.revision_id,
            "parent_revision_id": None,
            "revision_number": 1,
        },
        {
            "id": candidate.candidate_revision_id,
            "parent_revision_id": initial.revision_id,
            "revision_number": 2,
        },
    ]
    assert change_set_status == "pending_review"

    with pytest.raises(Exception, match="immutable|permission denied"):
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            await connection.execute(
                text(
                    "UPDATE project_revisions SET revision_number=99 WHERE id=:id"
                ),
                {"id": candidate.candidate_revision_id},
            )

    with pytest.raises(Exception, match="immutable"):
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            await connection.execute(
                text(
                    "UPDATE change_sets SET objective='tampered' WHERE id=:id"
                ),
                {"id": candidate.change_set_id},
            )

    with pytest.raises(Exception, match="immutable|permission denied"):
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            await connection.execute(
                text("DELETE FROM project_revisions WHERE id=:id"),
                {"id": candidate.candidate_revision_id},
            )


@pytest.mark.asyncio(loop_scope="module")
async def test_change_set_idempotency_replays_and_rejects_different_candidate():
    owner, project_id = await _seed_project()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        initial = await create_initial_branch(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            created_by_principal_id=owner.principal_id,
            branch_name="main",
            initial_manifest=_manifest(10),
        )
        first = await create_candidate_change_set(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            created_by_principal_id=owner.principal_id,
            idempotency_key="candidate-idempotency",
            objective="调整宽度",
            candidate_manifest=_manifest(11),
        )
        replay = await create_candidate_change_set(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            created_by_principal_id=owner.principal_id,
            idempotency_key="candidate-idempotency",
            objective="调整宽度",
            candidate_manifest=_manifest(11),
        )
        with pytest.raises(ValueError, match="idempotency"):
            await create_candidate_change_set(
                connection,
                tenant_id=owner.tenant_id,
                project_id=project_id,
                branch_id=initial.branch_id,
                expected_base_revision_id=initial.revision_id,
                created_by_principal_id=owner.principal_id,
                idempotency_key="candidate-idempotency",
                objective="不同修改",
                candidate_manifest=_manifest(12),
            )
        counts = (
            await connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM change_sets WHERE branch_id=:branch_id) "
                    "AS changes, "
                    "(SELECT count(*) FROM project_revisions WHERE branch_id=:branch_id) "
                    "AS revisions"
                ),
                {"branch_id": initial.branch_id},
            )
        ).mappings().one()

    assert replay.replayed is True
    assert replay.change_set_id == first.change_set_id
    assert replay.candidate_revision_id == first.candidate_revision_id
    assert counts == {"changes": 1, "revisions": 2}


@pytest.mark.asyncio(loop_scope="module")
async def test_concurrent_branch_cas_has_one_winner_and_preserves_both_candidates():
    owner, project_id = await _seed_project()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        initial = await create_initial_branch(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            created_by_principal_id=owner.principal_id,
            branch_name="main",
            initial_manifest=_manifest(30),
        )
        first = await create_candidate_change_set(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            created_by_principal_id=owner.principal_id,
            idempotency_key="candidate-a",
            objective="A",
            candidate_manifest=_manifest(31),
        )
        second = await create_candidate_change_set(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            created_by_principal_id=owner.principal_id,
            idempotency_key="candidate-b",
            objective="B",
            candidate_manifest=_manifest(32),
        )

    async def advance(candidate_revision_id):
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            return await compare_and_swap_branch_head(
                connection,
                tenant_id=owner.tenant_id,
                project_id=project_id,
                branch_id=initial.branch_id,
                expected_head_revision_id=initial.revision_id,
                candidate_revision_id=candidate_revision_id,
            )

    outcomes = await asyncio.gather(
        advance(first.candidate_revision_id),
        advance(second.candidate_revision_id),
    )
    assert sorted(outcomes) == [False, True]

    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        head = await connection.scalar(
            text("SELECT head_revision_id FROM project_branches WHERE id=:id"),
            {"id": initial.branch_id},
        )
        candidates = (
            await connection.execute(
                text(
                    "SELECT candidate_revision_id, status FROM change_sets "
                    "WHERE branch_id=:branch_id ORDER BY objective"
                ),
                {"branch_id": initial.branch_id},
            )
        ).mappings().all()
        with pytest.raises(StaleBaseRevision):
            await create_candidate_change_set(
                connection,
                tenant_id=owner.tenant_id,
                project_id=project_id,
                branch_id=initial.branch_id,
                expected_base_revision_id=initial.revision_id,
                created_by_principal_id=owner.principal_id,
                idempotency_key="stale-candidate",
                objective="stale",
                candidate_manifest=_manifest(33),
            )
        initial_replay = await create_initial_branch(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            created_by_principal_id=owner.principal_id,
            branch_name="main",
            initial_manifest=_manifest(30),
        )

    assert head in {
        first.candidate_revision_id,
        second.candidate_revision_id,
    }
    assert {row["candidate_revision_id"] for row in candidates} == {
        first.candidate_revision_id,
        second.candidate_revision_id,
    }
    # Task 4 only moves the primitive head; review status is not accepted yet.
    assert {row["status"] for row in candidates} == {"pending_review"}
    assert initial_replay.replayed is True
    assert initial_replay.revision_id == initial.revision_id


@pytest.mark.asyncio(loop_scope="module")
async def test_revision_branch_and_change_set_tables_force_tenant_rls():
    expected_tables = {
        "project_branches",
        "project_revisions",
        "change_sets",
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
async def test_cross_tenant_revision_ids_are_invisible():
    first_owner, first_project = await _seed_project()
    second_owner, second_project = await _seed_project()

    async with tenant_transaction(
        second_owner.tenant_id,
        second_owner.principal_id,
    ) as connection:
        second = await create_initial_branch(
            connection,
            tenant_id=second_owner.tenant_id,
            project_id=second_project,
            created_by_principal_id=second_owner.principal_id,
            branch_name="main",
            initial_manifest=_manifest(50),
        )

    async with tenant_transaction(
        first_owner.tenant_id,
        first_owner.principal_id,
    ) as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM project_revisions WHERE id=:id"),
                {"id": second.revision_id},
            )
        ) == 0
        with pytest.raises(Exception, match="row-level security|foreign key"):
            await connection.execute(
                text(
                    "INSERT INTO project_branches "
                    "(id, tenant_id, project_id, name, created_by_principal_id) "
                    "VALUES (:id, :tenant_id, :project_id, 'cross', :principal_id)"
                ),
                {
                    "id": uuid4(),
                    "tenant_id": second_owner.tenant_id,
                    "project_id": second_project,
                    "principal_id": second_owner.principal_id,
                },
            )
