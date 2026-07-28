"""Real PostgreSQL state-machine, idempotency, and lease-fencing tests."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
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
from app.domain.runs import (
    ATTEMPT_TRANSITIONS,
    STEP_TRANSITIONS,
    WORKFLOW_TRANSITIONS,
    AttemptStatus,
    StepStatus,
    WorkflowStatus,
)
from app.execution.canonical import canonical_sha256
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.services.run_state import (
    IdempotencyConflict,
    IllegalTransition,
    StaleLease,
    complete_attempt,
    create_attempt,
    create_step,
    create_workflow,
    heartbeat_attempt,
    lease_attempt,
    request_workflow_cancellation,
    start_attempt,
    transition_attempt,
    transition_step,
    transition_workflow,
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
                "TRUNCATE outbox_messages, task_events, execution_attempts, "
                "step_runs, workflow_runs, usage_meter_entries, audit_records, "
                "project_memberships, projects, tenant_memberships, principals, "
                "tenants CASCADE"
            )
        )
    yield


async def _seed_project():
    owner = user_principal(f"run-owner-{uuid4()}")
    project_id = uuid4()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        await ensure_principal(connection, owner)
        await create_project(
            connection,
            project_id=project_id,
            tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id,
            name="Durable MCAD",
            slug=f"durable-{project_id.hex[:8]}",
        )
    return owner, project_id


def test_transition_matrices_cover_every_state_and_keep_terminals_closed():
    assert set(WORKFLOW_TRANSITIONS) == set(WorkflowStatus)
    assert set(STEP_TRANSITIONS) == set(StepStatus)
    assert set(ATTEMPT_TRANSITIONS) == set(AttemptStatus)
    for terminal in (
        WorkflowStatus.SUCCEEDED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.TIMED_OUT,
    ):
        assert WORKFLOW_TRANSITIONS[terminal] == frozenset()
    for terminal in (
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED,
        AttemptStatus.CANCELLED,
        AttemptStatus.TIMED_OUT,
    ):
        assert ATTEMPT_TRANSITIONS[terminal] == frozenset()


@pytest.mark.asyncio(loop_scope="module")
async def test_workflow_idempotency_hash_replays_and_rejects_payload_reuse():
    owner, project_id = await _seed_project()
    payload = {"prompt": "创建齿轮箱", "base_revision_id": None}

    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="generate",
            idempotency_key="workflow-generate-1",
            request_payload=payload,
        )
        replayed = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="generate",
            idempotency_key="workflow-generate-1",
            request_payload=payload,
        )
        with pytest.raises(IdempotencyConflict):
            await create_workflow(
                connection,
                tenant_id=owner.tenant_id,
                project_id=project_id,
                requested_by_principal_id=owner.principal_id,
                kind="generate",
                idempotency_key="workflow-generate-1",
                request_payload={"prompt": "不同请求"},
            )

        event_count = await connection.scalar(
            text("SELECT count(*) FROM task_events WHERE workflow_run_id=:id"),
            {"id": created.workflow_id},
        )

    assert replayed.workflow_id == created.workflow_id
    assert created.replayed is False
    assert replayed.replayed is True
    assert event_count == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_only_approved_workflow_and_step_transitions_are_accepted():
    owner, project_id = await _seed_project()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        workflow = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="modify",
            idempotency_key="workflow-transitions",
            request_payload={"instruction": "孔径改为 4mm"},
        )
        await transition_workflow(
            connection,
            workflow.workflow_id,
            expected=WorkflowStatus.PENDING,
            target=WorkflowStatus.PLANNING,
        )
        await transition_workflow(
            connection,
            workflow.workflow_id,
            expected=WorkflowStatus.PLANNING,
            target=WorkflowStatus.RUNNING,
        )
        with pytest.raises(IllegalTransition):
            await transition_workflow(
                connection,
                workflow.workflow_id,
                expected=WorkflowStatus.RUNNING,
                target=WorkflowStatus.PENDING,
            )

        step = await create_step(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_key="model",
            step_index=1,
            kind="mcad_model",
        )
        await transition_step(
            connection,
            step.step_id,
            expected=StepStatus.PENDING,
            target=StepStatus.READY,
        )
        await transition_step(
            connection,
            step.step_id,
            expected=StepStatus.READY,
            target=StepStatus.RUNNING,
        )
        with pytest.raises(IllegalTransition):
            await transition_step(
                connection,
                step.step_id,
                expected=StepStatus.RUNNING,
                target=StepStatus.PENDING,
            )
        await transition_step(
            connection,
            step.step_id,
            expected=StepStatus.RUNNING,
            target=StepStatus.FAILED,
            error_code="first_attempt_failed",
        )
        failed_at = await connection.scalar(
            text("SELECT completed_at FROM step_runs WHERE id=:id"),
            {"id": step.step_id},
        )
        await transition_step(
            connection,
            step.step_id,
            expected=StepStatus.FAILED,
            target=StepStatus.READY,
        )

        stored = (
            await connection.execute(
                text(
                    "SELECT w.status AS workflow_status, s.status AS step_status, "
                    "s.completed_at AS step_completed_at "
                    "FROM workflow_runs w JOIN step_runs s "
                    "ON s.workflow_run_id=w.id "
                    "WHERE w.id=:workflow_id"
                ),
                {"workflow_id": workflow.workflow_id},
            )
        ).mappings().one()

    assert stored["workflow_status"] == WorkflowStatus.RUNNING.value
    assert failed_at is not None
    assert stored["step_status"] == StepStatus.READY.value
    assert stored["step_completed_at"] is None


@pytest.mark.asyncio(loop_scope="module")
async def test_retry_creates_a_new_immutable_attempt_instead_of_overwriting():
    owner, project_id = await _seed_project()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        workflow = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="generate",
            idempotency_key="workflow-retry",
            request_payload={"prompt": "轴承座"},
        )
        step = await create_step(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_key="model",
            step_index=1,
            kind="mcad_model",
        )
        first = await create_attempt(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_id=step.step_id,
            idempotency_key="attempt-one",
            execution_payload={"runtime": "cadquery", "code_hash": "a" * 64},
        )
        await transition_attempt(
            connection,
            first.attempt_id,
            expected=AttemptStatus.PENDING,
            target=AttemptStatus.FAILED,
            error_code="worker_failed",
        )
        second = await create_attempt(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_id=step.step_id,
            idempotency_key="attempt-two",
            execution_payload={"runtime": "cadquery", "code_hash": "a" * 64},
        )
        attempts = (
            await connection.execute(
                text(
                    "SELECT id, attempt_number, status FROM execution_attempts "
                    "WHERE step_run_id=:step_id ORDER BY attempt_number"
                ),
                {"step_id": step.step_id},
            )
        ).mappings().all()

    assert first.attempt_id != second.attempt_id
    assert [(row["attempt_number"], row["status"]) for row in attempts] == [
        (1, AttemptStatus.FAILED.value),
        (2, AttemptStatus.PENDING.value),
    ]


@pytest.mark.asyncio(loop_scope="module")
async def test_lease_generation_fences_stale_workers_and_completion_replays_once():
    owner, project_id = await _seed_project()
    base_time = datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        workflow = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="generate",
            idempotency_key="workflow-lease",
            request_payload={"prompt": "夹具"},
        )
        step = await create_step(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_key="model",
            step_index=1,
            kind="mcad_model",
        )
        attempt = await create_attempt(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_id=step.step_id,
            idempotency_key="attempt-lease",
            execution_payload={"code_hash": "b" * 64},
        )
        first_lease = await lease_attempt(
            connection,
            attempt.attempt_id,
            worker_id="worker-a",
            now=base_time,
            lease_seconds=1,
        )
        second_lease = await lease_attempt(
            connection,
            attempt.attempt_id,
            worker_id="worker-b",
            now=base_time + timedelta(seconds=2),
            lease_seconds=30,
        )

        with pytest.raises(StaleLease):
            await start_attempt(
                connection,
                attempt.attempt_id,
                lease_token=first_lease.token,
                lease_generation=first_lease.generation,
                now=base_time + timedelta(seconds=2),
            )
        with pytest.raises(StaleLease):
            await complete_attempt(
                connection,
                attempt.attempt_id,
                lease_token=first_lease.token,
                lease_generation=first_lease.generation,
                result_payload={
                    "status": "success",
                    "artifact_claims": [{"name": "stale-worker.step"}],
                },
                now=base_time + timedelta(seconds=2),
            )
        await start_attempt(
            connection,
            attempt.attempt_id,
            lease_token=second_lease.token,
            lease_generation=second_lease.generation,
            now=base_time + timedelta(seconds=2),
        )
        await heartbeat_attempt(
            connection,
            attempt.attempt_id,
            lease_token=second_lease.token,
            lease_generation=second_lease.generation,
            now=base_time + timedelta(seconds=3),
            lease_seconds=30,
        )
        result = {
            "status": "success",
            "artifact_claims": [{"name": "result.step", "sha256": "c" * 64}],
        }
        completed = await complete_attempt(
            connection,
            attempt.attempt_id,
            lease_token=second_lease.token,
            lease_generation=second_lease.generation,
            result_payload=result,
            now=base_time + timedelta(seconds=4),
        )
        replayed = await complete_attempt(
            connection,
            attempt.attempt_id,
            lease_token=second_lease.token,
            lease_generation=second_lease.generation,
            result_payload=result,
            now=base_time + timedelta(seconds=5),
        )
        with pytest.raises(IdempotencyConflict):
            await complete_attempt(
                connection,
                attempt.attempt_id,
                lease_token=second_lease.token,
                lease_generation=second_lease.generation,
                result_payload={
                    "status": "success",
                    "artifact_claims": [{"name": "different.step"}],
                },
                now=base_time + timedelta(seconds=6),
            )
        stored = (
            await connection.execute(
                text(
                    "SELECT status, lease_generation, result_payload "
                    "FROM execution_attempts WHERE id=:id"
                ),
                {"id": attempt.attempt_id},
            )
        ).mappings().one()
        completed_events = await connection.scalar(
            text(
                "SELECT count(*) FROM task_events "
                "WHERE workflow_run_id=:workflow_id "
                "AND event_type='attempt.completed'"
            ),
            {"workflow_id": workflow.workflow_id},
        )

    assert second_lease.generation == first_lease.generation + 1
    assert completed.replayed is False
    assert replayed.replayed is True
    assert stored["status"] == AttemptStatus.SUCCEEDED.value
    assert stored["result_payload"] == result
    assert completed_events == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_real_worker_role_can_lease_heartbeat_and_complete_without_bypass():
    owner, project_id = await _seed_project()
    now = datetime.now(timezone.utc)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        workflow = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="validate",
            idempotency_key="workflow-worker-role",
            request_payload={"checks": ["geometry"]},
        )
        step = await create_step(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_key="validate",
            step_index=1,
            kind="geometry_validation",
        )
        attempt = await create_attempt(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_id=step.step_id,
            idempotency_key="attempt-worker-role",
            execution_payload={"check": "geometry"},
        )

    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
        role="worker",
    ) as connection:
        lease = await lease_attempt(
            connection,
            attempt.attempt_id,
            worker_id="private-worker-1",
            now=now,
            lease_seconds=30,
        )
        await start_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
            now=now,
        )
        await heartbeat_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
            now=now + timedelta(seconds=1),
            lease_seconds=30,
        )
        await complete_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
            result_payload={"status": "success", "checks_passed": 1},
            now=now + timedelta(seconds=2),
        )

    with pytest.raises(Exception, match="permission denied"):
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
            role="worker",
        ) as connection:
            await connection.execute(
                text(
                    "UPDATE workflow_runs SET status='failed' WHERE id=:id"
                ),
                {"id": workflow.workflow_id},
            )

    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        stored = (
            await connection.execute(
                text(
                    "SELECT status, worker_id, lease_token_hash "
                    "FROM execution_attempts WHERE id=:id"
                ),
                {"id": attempt.attempt_id},
            )
        ).mappings().one()
    assert stored["status"] == AttemptStatus.SUCCEEDED.value
    assert stored["worker_id"] == "private-worker-1"
    assert stored["lease_token_hash"] != lease.token
    assert lease.token not in str(stored)
    assert lease.token not in repr(lease)


@pytest.mark.asyncio(loop_scope="module")
async def test_attempt_identity_is_immutable_and_timeout_is_terminal():
    owner, project_id = await _seed_project()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        workflow = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="generate",
            idempotency_key="workflow-immutable-attempt",
            request_payload={"prompt": "法兰"},
        )
        step = await create_step(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_key="model",
            step_index=1,
            kind="mcad_model",
        )
        attempt = await create_attempt(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_id=step.step_id,
            idempotency_key="attempt-immutable",
            execution_payload={"code_hash": "d" * 64},
        )

    with pytest.raises(Exception, match="immutable"):
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            await connection.execute(
                text(
                    "UPDATE execution_attempts "
                    "SET execution_payload_hash=:hash WHERE id=:id"
                ),
                {"id": attempt.attempt_id, "hash": "e" * 64},
            )

    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        await transition_attempt(
            connection,
            attempt.attempt_id,
            expected=AttemptStatus.PENDING,
            target=AttemptStatus.TIMED_OUT,
            error_code="lease_timeout",
        )
        with pytest.raises(IllegalTransition):
            await lease_attempt(
                connection,
                attempt.attempt_id,
                worker_id="late-worker",
            )
        stored = (
            await connection.execute(
                text(
                    "SELECT status, execution_payload_hash "
                    "FROM execution_attempts WHERE id=:id"
                ),
                {"id": attempt.attempt_id},
            )
        ).mappings().one()

    assert stored["status"] == AttemptStatus.TIMED_OUT.value
    assert stored["execution_payload_hash"] == canonical_sha256(
        {"code_hash": "d" * 64}
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_cancellation_intent_blocks_late_worker_completion():
    owner, project_id = await _seed_project()
    now = datetime.now(timezone.utc)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        workflow = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="export",
            idempotency_key="workflow-cancel",
            request_payload={"format": "STEP"},
        )
        await transition_workflow(
            connection,
            workflow.workflow_id,
            expected=WorkflowStatus.PENDING,
            target=WorkflowStatus.PLANNING,
        )
        await transition_workflow(
            connection,
            workflow.workflow_id,
            expected=WorkflowStatus.PLANNING,
            target=WorkflowStatus.RUNNING,
        )
        step = await create_step(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_key="export",
            step_index=1,
            kind="export_step",
        )
        attempt = await create_attempt(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_id=step.step_id,
            idempotency_key="attempt-cancel",
            execution_payload={"format": "STEP"},
        )
        lease = await lease_attempt(
            connection,
            attempt.attempt_id,
            worker_id="worker-cancel",
            now=now,
            lease_seconds=30,
        )
        await start_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
            now=now,
        )
        await request_workflow_cancellation(
            connection,
            workflow.workflow_id,
            now=now + timedelta(seconds=1),
        )
        with pytest.raises(IllegalTransition, match="cancellation"):
            await complete_attempt(
                connection,
                attempt.attempt_id,
                lease_token=lease.token,
                lease_generation=lease.generation,
                result_payload={
                    "status": "success",
                    "artifact_claims": [{"name": "late.step"}],
                },
                now=now + timedelta(seconds=2),
            )
        stored = (
            await connection.execute(
                text(
                    "SELECT a.status, a.result_payload, w.status AS workflow_status "
                    "FROM execution_attempts a JOIN workflow_runs w "
                    "ON w.id=a.workflow_run_id WHERE a.id=:attempt_id"
                ),
                {"attempt_id": attempt.attempt_id},
            )
        ).mappings().one()

    assert stored["status"] == AttemptStatus.RUNNING.value
    assert stored["result_payload"] is None
    assert stored["workflow_status"] == WorkflowStatus.CANCELLING.value
