"""Transactional durable-run state machine and lease fencing."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.runs import (
    ATTEMPT_TRANSITIONS,
    STEP_TRANSITIONS,
    WORKFLOW_TRANSITIONS,
    AttemptCompletion,
    AttemptCreated,
    AttemptLease,
    AttemptStatus,
    StepCreated,
    StepStatus,
    WorkflowCreated,
    WorkflowStatus,
)
from app.execution.canonical import canonical_sha256
from app.execution.contracts import ExecutionError
from app.repositories.runs import append_workflow_event


class RunStateError(RuntimeError):
    """Base class for durable state failures."""


class IdempotencyConflict(RunStateError):
    """An idempotency key was reused for a different immutable request."""


class IllegalTransition(RunStateError):
    """The requested lifecycle transition is not allowed."""


class StaleLease(RunStateError):
    """A worker supplied an expired, replaced, or otherwise invalid lease."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _terminal_timestamp(status: str, terminal: set[str], now: datetime):
    return now if status in terminal else None


async def create_workflow(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    requested_by_principal_id: UUID,
    kind: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
) -> WorkflowCreated:
    payload_hash = canonical_sha256(request_payload)
    workflow_id = uuid4()
    inserted_id = await connection.scalar(
        text(
            """
            INSERT INTO workflow_runs (
                id, tenant_id, project_id, requested_by_principal_id, kind,
                idempotency_key, request_payload_hash, request_payload
            )
            VALUES (
                :id, :tenant_id, :project_id, :requested_by, :kind,
                :idempotency_key, :payload_hash, CAST(:payload AS jsonb)
            )
            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": workflow_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "requested_by": requested_by_principal_id,
            "kind": kind,
            "idempotency_key": idempotency_key,
            "payload_hash": payload_hash,
            "payload": _json(request_payload),
        },
    )
    if inserted_id is None:
        existing = (
            await connection.execute(
                text(
                    """
                    SELECT id, project_id, requested_by_principal_id, kind,
                           request_payload_hash
                    FROM workflow_runs
                    WHERE tenant_id=:tenant_id
                      AND idempotency_key=:idempotency_key
                    """
                ),
                {"tenant_id": tenant_id, "idempotency_key": idempotency_key},
            )
        ).mappings().one_or_none()
        if existing is None:
            raise RuntimeError("workflow idempotency replay could not be resolved")
        expected = {
            "project_id": project_id,
            "requested_by_principal_id": requested_by_principal_id,
            "kind": kind,
            "request_payload_hash": payload_hash,
        }
        if any(existing[field] != value for field, value in expected.items()):
            raise IdempotencyConflict(
                "workflow idempotency key was reused with a different payload"
            )
        return WorkflowCreated(workflow_id=existing["id"], replayed=True)

    from app.services.cloud_documents import enqueue_operation
    await enqueue_operation(
        connection, workflow_id=inserted_id, tenant_id=tenant_id,
        principal_id=requested_by_principal_id, payload=request_payload,
        idempotency_key=idempotency_key, request_hash=payload_hash,
    )
    await append_workflow_event(
        connection,
        tenant_id=tenant_id,
        workflow_id=inserted_id,
        event_type="workflow.created",
        payload={"kind": kind, "status": WorkflowStatus.PENDING.value},
    )
    rebase = request_payload.get("operation_context") or {}
    if rebase.get("rebased_from_revision_id"):
        await append_workflow_event(connection, tenant_id=tenant_id, workflow_id=workflow_id,
            event_type="document.operation_rebased", payload={k:rebase[k] for k in (
                "rebased_from_revision_id","rebased_from_state_version","rebase_evidence_hash","base_revision_id")})
    return WorkflowCreated(workflow_id=inserted_id)


async def transition_workflow(
    connection: AsyncConnection,
    workflow_id: UUID,
    *,
    expected: WorkflowStatus,
    target: WorkflowStatus,
    error_code: str | None = None,
    error_message: str | None = None,
    now: datetime | None = None,
) -> None:
    if target not in WORKFLOW_TRANSITIONS[expected]:
        raise IllegalTransition(
            f"workflow transition {expected.value} -> {target.value} is not allowed"
        )
    current_time = now or _utcnow()
    row = (
        await connection.execute(
            text(
                "SELECT tenant_id, status FROM workflow_runs "
                "WHERE id=:id FOR UPDATE"
            ),
            {"id": workflow_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise KeyError(workflow_id)
    if row["status"] != expected.value:
        raise IllegalTransition(
            f"workflow expected {expected.value}, found {row['status']}"
        )
    terminal = {
        WorkflowStatus.SUCCEEDED.value,
        WorkflowStatus.FAILED.value,
        WorkflowStatus.CANCELLED.value,
        WorkflowStatus.TIMED_OUT.value,
    }
    await connection.execute(
        text(
            """
            UPDATE workflow_runs
            SET status=:status,
                started_at=CASE
                    WHEN :status IN ('planning', 'running')
                    THEN COALESCE(started_at, :now)
                    ELSE started_at
                END,
                completed_at=COALESCE(:completed_at, completed_at),
                error_code=:error_code,
                error_message=:error_message,
                updated_at=:now
            WHERE id=:id
            """
        ),
        {
            "id": workflow_id,
            "status": target.value,
            "now": current_time,
            "completed_at": _terminal_timestamp(
                target.value,
                terminal,
                current_time,
            ),
            "error_code": error_code,
            "error_message": error_message,
        },
    )
    await append_workflow_event(
        connection,
        tenant_id=row["tenant_id"],
        workflow_id=workflow_id,
        event_type="workflow.state_changed",
        payload={
            "previous_status": expected.value,
            "status": target.value,
            "error_code": error_code,
            "error_message": error_message,
        },
    )


async def create_step(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    workflow_id: UUID,
    step_key: str,
    step_index: int,
    kind: str,
) -> StepCreated:
    # Serialize step-key allocation and its event sequence on the parent run.
    # This turns concurrent Temporal/activity replays into one create + replays
    # instead of leaking a uniqueness error from the database.
    workflow_exists = await connection.scalar(
        text(
            "SELECT id FROM workflow_runs "
            "WHERE tenant_id=:tenant_id AND id=:workflow_id FOR UPDATE"
        ),
        {"tenant_id": tenant_id, "workflow_id": workflow_id},
    )
    if workflow_exists is None:
        raise KeyError(workflow_id)
    existing = (
        await connection.execute(
            text(
                """
                SELECT id, step_index, kind FROM step_runs
                WHERE tenant_id=:tenant_id
                  AND workflow_run_id=:workflow_id
                  AND step_key=:step_key
                """
            ),
            {
                "tenant_id": tenant_id,
                "workflow_id": workflow_id,
                "step_key": step_key,
            },
        )
    ).mappings().one_or_none()
    if existing:
        if existing["step_index"] != step_index or existing["kind"] != kind:
            raise IdempotencyConflict(
                "step key was reused with a different index or kind"
            )
        return StepCreated(step_id=existing["id"], replayed=True)

    step_id = uuid4()
    await connection.execute(
        text(
            """
            INSERT INTO step_runs (
                id, tenant_id, workflow_run_id, step_key, step_index, kind
            )
            VALUES (
                :id, :tenant_id, :workflow_id, :step_key, :step_index, :kind
            )
            """
        ),
        {
            "id": step_id,
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "step_key": step_key,
            "step_index": step_index,
            "kind": kind,
        },
    )
    await append_workflow_event(
        connection,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        event_type="step.created",
        payload={
            "step_id": str(step_id),
            "step_key": step_key,
            "step_index": step_index,
            "kind": kind,
        },
    )
    return StepCreated(step_id=step_id)


async def transition_step(
    connection: AsyncConnection,
    step_id: UUID,
    *,
    expected: StepStatus,
    target: StepStatus,
    error_code: str | None = None,
    error_message: str | None = None,
    error: ExecutionError | dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    if target not in STEP_TRANSITIONS[expected]:
        raise IllegalTransition(
            f"step transition {expected.value} -> {target.value} is not allowed"
        )
    current_time = now or _utcnow()
    structured_error = (
        ExecutionError.model_validate(error) if error is not None else None
    )
    if structured_error is not None:
        if error_code is not None and error_code != structured_error.code:
            raise ValueError("scalar and structured step error codes differ")
        if error_message is not None and error_message != structured_error.message:
            raise ValueError("scalar and structured step error messages differ")
        error_code = structured_error.code
        error_message = structured_error.message
    error_details = (
        structured_error.model_dump(mode="json") if structured_error else {}
    )
    row = (
        await connection.execute(
            text(
                "SELECT tenant_id, workflow_run_id, status FROM step_runs "
                "WHERE id=:id FOR UPDATE"
            ),
            {"id": step_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise KeyError(step_id)
    if row["status"] != expected.value:
        raise IllegalTransition(
            f"step expected {expected.value}, found {row['status']}"
        )
    await connection.execute(
        text(
            """
            UPDATE step_runs
            SET status=:status,
                started_at=CASE
                    WHEN :status='running' THEN COALESCE(started_at, :now)
                    ELSE started_at
                END,
                completed_at=CASE
                    WHEN :status IN (
                        'succeeded', 'failed', 'skipped', 'cancelled', 'timed_out'
                    ) THEN :now
                    WHEN :status IN ('ready', 'running') THEN NULL
                    ELSE completed_at
                END,
                error_code=:error_code,
                error_message=:error_message,
                error_details=CAST(:error_details AS jsonb),
                updated_at=:now
            WHERE id=:id
            """
        ),
        {
            "id": step_id,
            "status": target.value,
            "now": current_time,
            "error_code": error_code,
            "error_message": error_message,
            "error_details": _json(error_details),
        },
    )
    await append_workflow_event(
        connection,
        tenant_id=row["tenant_id"],
        workflow_id=row["workflow_run_id"],
        event_type="step.state_changed",
        payload={
            "step_id": str(step_id),
            "previous_status": expected.value,
            "status": target.value,
            "error_code": error_code,
            "error_message": error_message,
            "error": error_details or None,
        },
    )


async def create_attempt(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    workflow_id: UUID,
    step_id: UUID,
    idempotency_key: str,
    execution_payload: dict[str, Any],
) -> AttemptCreated:
    payload_hash = canonical_sha256(execution_payload)
    step = (
        await connection.execute(
            text(
                """
                SELECT attempt_count FROM step_runs
                WHERE tenant_id=:tenant_id
                  AND workflow_run_id=:workflow_id
                  AND id=:step_id
                FOR UPDATE
                """
            ),
            {
                "tenant_id": tenant_id,
                "workflow_id": workflow_id,
                "step_id": step_id,
            },
        )
    ).mappings().one_or_none()
    if step is None:
        raise KeyError(step_id)
    existing = (
        await connection.execute(
            text(
                """
                SELECT id, workflow_run_id, step_run_id, attempt_number,
                       execution_payload_hash
                FROM execution_attempts
                WHERE tenant_id=:tenant_id
                  AND idempotency_key=:idempotency_key
                """
            ),
            {"tenant_id": tenant_id, "idempotency_key": idempotency_key},
        )
    ).mappings().one_or_none()
    if existing:
        if (
            existing["workflow_run_id"] != workflow_id
            or existing["step_run_id"] != step_id
            or existing["execution_payload_hash"] != payload_hash
        ):
            raise IdempotencyConflict(
                "attempt idempotency key was reused with a different payload"
            )
        return AttemptCreated(
            attempt_id=existing["id"],
            attempt_number=existing["attempt_number"],
            replayed=True,
        )

    attempt_number = int(step["attempt_count"]) + 1
    await connection.execute(
        text(
            """
            UPDATE step_runs
            SET attempt_count=:attempt_number, updated_at=CURRENT_TIMESTAMP
            WHERE id=:step_id
            """
        ),
        {"attempt_number": attempt_number, "step_id": step_id},
    )
    attempt_id = uuid4()
    await connection.execute(
        text(
            """
            INSERT INTO execution_attempts (
                id, tenant_id, workflow_run_id, step_run_id, attempt_number,
                idempotency_key, execution_payload_hash, execution_payload
            )
            VALUES (
                :id, :tenant_id, :workflow_id, :step_id, :attempt_number,
                :idempotency_key, :payload_hash, CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "id": attempt_id,
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "step_id": step_id,
            "attempt_number": attempt_number,
            "idempotency_key": idempotency_key,
            "payload_hash": payload_hash,
            "payload": _json(execution_payload),
        },
    )
    await append_workflow_event(
        connection,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        event_type="attempt.created",
        payload={
            "attempt_id": str(attempt_id),
            "step_id": str(step_id),
            "attempt_number": attempt_number,
        },
    )
    return AttemptCreated(
        attempt_id=attempt_id,
        attempt_number=attempt_number,
    )


async def transition_attempt(
    connection: AsyncConnection,
    attempt_id: UUID,
    *,
    expected: AttemptStatus,
    target: AttemptStatus,
    error_code: str | None = None,
    error_message: str | None = None,
    error: ExecutionError | dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    if target not in ATTEMPT_TRANSITIONS[expected]:
        raise IllegalTransition(
            f"attempt transition {expected.value} -> {target.value} is not allowed"
        )
    current_time = now or _utcnow()
    structured_error = (
        ExecutionError.model_validate(error) if error is not None else None
    )
    if structured_error is not None:
        if error_code is not None and error_code != structured_error.code:
            raise ValueError("scalar and structured attempt error codes differ")
        if error_message is not None and error_message != structured_error.message:
            raise ValueError("scalar and structured attempt error messages differ")
        error_code = structured_error.code
        error_message = structured_error.message
    error_details = (
        structured_error.model_dump(mode="json") if structured_error else {}
    )
    row = (
        await connection.execute(
            text(
                "SELECT tenant_id, workflow_run_id, status "
                "FROM execution_attempts WHERE id=:id FOR UPDATE"
            ),
            {"id": attempt_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise KeyError(attempt_id)
    if row["status"] != expected.value:
        raise IllegalTransition(
            f"attempt expected {expected.value}, found {row['status']}"
        )
    await connection.execute(
        text(
            """
            UPDATE execution_attempts
            SET status=:status,
                completed_at=CASE
                    WHEN :status IN ('succeeded', 'failed', 'cancelled', 'timed_out')
                    THEN :now ELSE completed_at
                END,
                error_code=:error_code,
                error_message=:error_message,
                error_details=CAST(:error_details AS jsonb),
                updated_at=:now
            WHERE id=:id
            """
        ),
        {
            "id": attempt_id,
            "status": target.value,
            "now": current_time,
            "error_code": error_code,
            "error_message": error_message,
            "error_details": _json(error_details),
        },
    )
    await append_workflow_event(
        connection,
        tenant_id=row["tenant_id"],
        workflow_id=row["workflow_run_id"],
        event_type="attempt.state_changed",
        payload={
            "attempt_id": str(attempt_id),
            "previous_status": expected.value,
            "status": target.value,
            "error_code": error_code,
            "error_message": error_message,
            "error": error_details or None,
        },
    )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _assert_lease_identity(
    row,
    *,
    lease_token: str,
    lease_generation: int,
) -> None:
    stored_hash = row["lease_token_hash"] or ""
    if (
        row["lease_generation"] != lease_generation
        or not stored_hash
        or not hmac.compare_digest(stored_hash, _token_hash(lease_token))
    ):
        raise StaleLease("attempt lease token or generation is stale")


def _assert_active_lease(row, *, now: datetime) -> None:
    if row["leased_until"] is None or row["leased_until"] <= now:
        raise StaleLease("attempt lease has expired")


async def lease_attempt(
    connection: AsyncConnection,
    attempt_id: UUID,
    *,
    worker_id: str,
    now: datetime | None = None,
    lease_seconds: int = 30,
) -> AttemptLease:
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be at least 1")
    current_time = now or _utcnow()
    row = (
        await connection.execute(
            text(
                """
                SELECT tenant_id, workflow_run_id, status, lease_generation,
                       leased_until
                FROM execution_attempts WHERE id=:id FOR UPDATE
                """
            ),
            {"id": attempt_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise KeyError(attempt_id)
    claimable = row["status"] == AttemptStatus.PENDING.value or (
        row["status"]
        in {AttemptStatus.LEASED.value, AttemptStatus.RUNNING.value}
        and row["leased_until"] is not None
        and row["leased_until"] <= current_time
    )
    if not claimable:
        raise IllegalTransition(
            f"attempt in {row['status']} cannot be leased"
        )
    workflow_status = await connection.scalar(
        text("SELECT status FROM workflow_runs WHERE id=:id"),
        {"id": row["workflow_run_id"]},
    )
    if workflow_status in {
        WorkflowStatus.CANCELLING.value,
        WorkflowStatus.CANCELLED.value,
        WorkflowStatus.FAILED.value,
        WorkflowStatus.TIMED_OUT.value,
    }:
        raise IllegalTransition("workflow cancellation or termination blocks leasing")

    token = secrets.token_urlsafe(32)
    generation = int(row["lease_generation"]) + 1
    leased_until = current_time + timedelta(seconds=lease_seconds)
    await connection.execute(
        text(
            """
            UPDATE execution_attempts
            SET status='leased',
                lease_generation=:generation,
                lease_token_hash=:token_hash,
                worker_id=:worker_id,
                leased_until=:leased_until,
                last_heartbeat_at=:now,
                updated_at=:now
            WHERE id=:id
            """
        ),
        {
            "id": attempt_id,
            "generation": generation,
            "token_hash": _token_hash(token),
            "worker_id": worker_id,
            "leased_until": leased_until,
            "now": current_time,
        },
    )
    await append_workflow_event(
        connection,
        tenant_id=row["tenant_id"],
        workflow_id=row["workflow_run_id"],
        event_type="attempt.leased",
        payload={
            "attempt_id": str(attempt_id),
            "worker_id": worker_id,
            "lease_generation": generation,
            "leased_until": leased_until.isoformat(),
        },
    )
    return AttemptLease(
        attempt_id=attempt_id,
        generation=generation,
        leased_until_iso=leased_until.isoformat(),
        token=token,
    )


async def _locked_attempt(connection: AsyncConnection, attempt_id: UUID):
    return (
        await connection.execute(
            text(
                """
                SELECT id, tenant_id, workflow_run_id, status, lease_generation,
                       lease_token_hash, leased_until, result_payload_hash
                FROM execution_attempts WHERE id=:id FOR UPDATE
                """
            ),
            {"id": attempt_id},
        )
    ).mappings().one_or_none()


async def start_attempt(
    connection: AsyncConnection,
    attempt_id: UUID,
    *,
    lease_token: str,
    lease_generation: int,
    now: datetime | None = None,
) -> None:
    current_time = now or _utcnow()
    row = await _locked_attempt(connection, attempt_id)
    if row is None:
        raise KeyError(attempt_id)
    _assert_lease_identity(
        row,
        lease_token=lease_token,
        lease_generation=lease_generation,
    )
    if row["status"] == AttemptStatus.RUNNING.value:
        _assert_active_lease(row, now=current_time)
        return
    if row["status"] != AttemptStatus.LEASED.value:
        raise IllegalTransition(f"attempt in {row['status']} cannot start")
    _assert_active_lease(row, now=current_time)
    await connection.execute(
        text(
            """
            UPDATE execution_attempts
            SET status='running', started_at=COALESCE(started_at, :now),
                updated_at=:now
            WHERE id=:id
            """
        ),
        {"id": attempt_id, "now": current_time},
    )
    await append_workflow_event(
        connection,
        tenant_id=row["tenant_id"],
        workflow_id=row["workflow_run_id"],
        event_type="attempt.started",
        payload={
            "attempt_id": str(attempt_id),
            "lease_generation": lease_generation,
        },
    )


async def heartbeat_attempt(
    connection: AsyncConnection,
    attempt_id: UUID,
    *,
    lease_token: str,
    lease_generation: int,
    now: datetime | None = None,
    lease_seconds: int = 30,
) -> None:
    current_time = now or _utcnow()
    row = await _locked_attempt(connection, attempt_id)
    if row is None:
        raise KeyError(attempt_id)
    _assert_lease_identity(
        row,
        lease_token=lease_token,
        lease_generation=lease_generation,
    )
    _assert_active_lease(row, now=current_time)
    if row["status"] not in {
        AttemptStatus.LEASED.value,
        AttemptStatus.RUNNING.value,
    }:
        raise IllegalTransition(f"attempt in {row['status']} cannot heartbeat")
    leased_until = current_time + timedelta(seconds=lease_seconds)
    await connection.execute(
        text(
            """
            UPDATE execution_attempts
            SET last_heartbeat_at=:now, leased_until=:leased_until,
                updated_at=:now
            WHERE id=:id
            """
        ),
        {
            "id": attempt_id,
            "now": current_time,
            "leased_until": leased_until,
        },
    )
    await append_workflow_event(
        connection,
        tenant_id=row["tenant_id"],
        workflow_id=row["workflow_run_id"],
        event_type="attempt.heartbeat",
        payload={
            "attempt_id": str(attempt_id),
            "lease_generation": lease_generation,
            "leased_until": leased_until.isoformat(),
        },
    )


async def complete_attempt(
    connection: AsyncConnection,
    attempt_id: UUID,
    *,
    lease_token: str,
    lease_generation: int,
    result_payload: dict[str, Any],
    now: datetime | None = None,
) -> AttemptCompletion:
    current_time = now or _utcnow()
    result_hash = canonical_sha256(result_payload)
    row = await _locked_attempt(connection, attempt_id)
    if row is None:
        raise KeyError(attempt_id)
    _assert_lease_identity(
        row,
        lease_token=lease_token,
        lease_generation=lease_generation,
    )
    if row["status"] == AttemptStatus.SUCCEEDED.value:
        if row["result_payload_hash"] != result_hash:
            raise IdempotencyConflict(
                "attempt completion was replayed with a different payload"
            )
        return AttemptCompletion(attempt_id=attempt_id, replayed=True)
    if row["status"] not in {
        AttemptStatus.LEASED.value,
        AttemptStatus.RUNNING.value,
    }:
        raise IllegalTransition(f"attempt in {row['status']} cannot complete")
    _assert_active_lease(row, now=current_time)
    workflow = (
        await connection.execute(
            text(
                "SELECT status, cancellation_requested_at FROM workflow_runs "
                "WHERE id=:id FOR UPDATE"
            ),
            {"id": row["workflow_run_id"]},
        )
    ).mappings().one()
    if workflow["cancellation_requested_at"] is not None or workflow["status"] in {
        WorkflowStatus.CANCELLING.value,
        WorkflowStatus.CANCELLED.value,
    }:
        raise IllegalTransition("workflow cancellation blocks attempt completion")

    await connection.execute(
        text(
            """
            UPDATE execution_attempts
            SET status='succeeded',
                result_payload_hash=:result_hash,
                result_payload=CAST(:result_payload AS jsonb),
                completed_at=:now,
                updated_at=:now
            WHERE id=:id
            """
        ),
        {
            "id": attempt_id,
            "result_hash": result_hash,
            "result_payload": _json(result_payload),
            "now": current_time,
        },
    )
    await append_workflow_event(
        connection,
        tenant_id=row["tenant_id"],
        workflow_id=row["workflow_run_id"],
        event_type="attempt.completed",
        payload={
            "attempt_id": str(attempt_id),
            "lease_generation": lease_generation,
            "result_hash": result_hash,
        },
    )
    return AttemptCompletion(attempt_id=attempt_id, replayed=False)


async def request_workflow_cancellation(
    connection: AsyncConnection,
    workflow_id: UUID,
    *,
    now: datetime | None = None,
) -> bool:
    current_time = now or _utcnow()
    row = (
        await connection.execute(
            text(
                "SELECT tenant_id, status, cancellation_requested_at "
                "FROM workflow_runs WHERE id=:id FOR UPDATE"
            ),
            {"id": workflow_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise KeyError(workflow_id)
    if row["cancellation_requested_at"] is not None:
        return False
    status = WorkflowStatus(row["status"])
    if status == WorkflowStatus.PENDING:
        target = WorkflowStatus.CANCELLED
    elif WorkflowStatus.CANCELLING in WORKFLOW_TRANSITIONS[status]:
        target = WorkflowStatus.CANCELLING
    else:
        raise IllegalTransition(
            f"workflow in {status.value} cannot request cancellation"
        )
    await connection.execute(
        text(
            """
            UPDATE workflow_runs
            SET status=:status, cancellation_requested_at=:now,
                completed_at=CASE WHEN :status='cancelled' THEN :now ELSE completed_at END,
                updated_at=:now
            WHERE id=:id
            """
        ),
        {"id": workflow_id, "status": target.value, "now": current_time},
    )
    await append_workflow_event(
        connection,
        tenant_id=row["tenant_id"],
        workflow_id=workflow_id,
        event_type="workflow.cancellation_requested",
        payload={
            "previous_status": status.value,
            "status": target.value,
        },
    )
    return True
