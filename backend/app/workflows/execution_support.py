"""Execution support shared by narrow activity handlers."""
from __future__ import annotations
import asyncio
import json
import os
import socket
from pathlib import Path
from typing import Any
from uuid import UUID
from sqlalchemy import text
from temporalio import activity
from temporalio.exceptions import ApplicationError
from app.agent.durable_plan import AgentPlanStep
from app.db import tenant_transaction
from app.domain.runs import AttemptStatus, StepStatus
from app.execution.backend import ExecutionBackend, MaterializedExecutionOutcome
from app.execution.contracts import ExecutionError, ExecutionSpec, ExecutionStatus, OutputDeclaration
from app.services.run_state import create_attempt, create_step, heartbeat_attempt, lease_attempt, start_attempt, transition_attempt, transition_step
from app.workflows.logical_steps import _step_row
from app.workflows.inputs import _uuid

def _worker_id() -> str:
    return f"temporal:{socket.gethostname()}:{os.getpid()}"


def _modeling_outputs(step: AgentPlanStep, mode: str) -> tuple[OutputDeclaration, ...]:
    formats = step.output_formats or (("dxf",) if mode == "2d" else ("step",))
    return tuple(
        OutputDeclaration(
            name=output_format,
            media_type=_MODELING_MEDIA_TYPES[output_format],
        )
        for output_format in formats
    )


async def _stored_execution_result(
    connection,
    *,
    workflow_id: UUID,
    step_key: str,
) -> dict[str, Any] | None:
    row = (
        await connection.execute(
            text(
                """
                SELECT s.id AS step_id, s.status AS step_status,
                       a.id AS attempt_id, a.result_payload
                FROM step_runs s
                JOIN execution_attempts a ON a.step_run_id=s.id
                WHERE s.workflow_run_id=:workflow_id
                  AND s.step_key=:step_key
                  AND a.status='succeeded'
                ORDER BY a.attempt_number DESC
                LIMIT 1
                FOR UPDATE OF s
                """
            ),
            {"workflow_id": workflow_id, "step_key": step_key},
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    status = StepStatus(row["step_status"])
    if status == StepStatus.RUNNING:
        await transition_step(
            connection,
            row["step_id"],
            expected=StepStatus.RUNNING,
            target=StepStatus.SUCCEEDED,
        )
    return {
        **dict(row["result_payload"] or {}),
        "attempt_id": str(row["attempt_id"]),
        "replayed": True,
    }


async def _prepare_execution_attempt(
    payload: dict[str, Any],
    *,
    temporal_attempt: int,
) -> tuple[UUID, UUID, str, int]:
    tenant_id = _uuid(payload, "tenant_id")
    principal_id = _uuid(payload, "principal_id")
    workflow_id = _uuid(payload, "workflow_run_id")
    execution = dict(payload["execution"])
    step_key = str(execution["step_key"])
    step_index = int(payload["step_index"])
    async with tenant_transaction(tenant_id, principal_id) as connection:
        replay = await _stored_execution_result(
            connection,
            workflow_id=workflow_id,
            step_key=step_key,
        )
        if replay is not None:
            return UUID(replay["attempt_id"]), UUID(int=0), "", 0

        created_step = await create_step(
            connection,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            step_key=step_key,
            step_index=step_index,
            kind=str(execution["kind"]),
        )
        row = await _step_row(connection, workflow_id, step_key)
        step_status = StepStatus(row["status"])

        # A Temporal retry is a new physical try. Fence every older non-terminal
        # attempt before creating it so a late worker cannot commit.
        active = (
            await connection.execute(
                text(
                    """
                    SELECT id, status FROM execution_attempts
                    WHERE workflow_run_id=:workflow_id
                      AND step_run_id=:step_id
                      AND status IN ('pending', 'leased', 'running')
                    ORDER BY attempt_number
                    FOR UPDATE
                    """
                ),
                {
                    "workflow_id": workflow_id,
                    "step_id": created_step.step_id,
                },
            )
        ).mappings().all()
        if active and (temporal_attempt > 1 or step_status == StepStatus.RUNNING):
            for attempt_row in active:
                await transition_attempt(
                    connection,
                    attempt_row["id"],
                    expected=AttemptStatus(attempt_row["status"]),
                    target=AttemptStatus.FAILED,
                    error_code="superseded_by_temporal_retry",
                    error_message="A newer Temporal activity attempt fenced this execution.",
                )
            if step_status == StepStatus.RUNNING:
                await transition_step(
                    connection,
                    created_step.step_id,
                    expected=StepStatus.RUNNING,
                    target=StepStatus.FAILED,
                    error_code="superseded_by_temporal_retry",
                )
                step_status = StepStatus.FAILED

        if step_status == StepStatus.PENDING:
            await transition_step(
                connection,
                created_step.step_id,
                expected=StepStatus.PENDING,
                target=StepStatus.READY,
            )
            step_status = StepStatus.READY
        if step_status in {StepStatus.FAILED, StepStatus.TIMED_OUT}:
            await transition_step(
                connection,
                created_step.step_id,
                expected=step_status,
                target=StepStatus.READY,
            )
            step_status = StepStatus.READY
        if step_status == StepStatus.READY:
            await transition_step(
                connection,
                created_step.step_id,
                expected=StepStatus.READY,
                target=StepStatus.RUNNING,
            )
        elif step_status != StepStatus.RUNNING:
            raise ApplicationError(
                f"step {step_key} is terminal in {step_status.value}",
                type="step_terminal",
                non_retryable=True,
            )

        execution_payload = {
            "schema_version": "temporal-mcad-execution.v1",
            "temporal_activity_id": activity.info().activity_id,
            "temporal_attempt": temporal_attempt,
            "execution": execution,
            "revision_id": str(payload["revision_id"]),
        }
        if payload.get("input_artifacts"):
            execution_payload["input_artifacts"] = payload["input_artifacts"]
        attempt = await create_attempt(
            connection,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            step_id=created_step.step_id,
            idempotency_key=(
                f"temporal:{workflow_id}:{step_key}:attempt:{temporal_attempt}"
            ),
            execution_payload=execution_payload,
        )
        lease = await lease_attempt(
            connection,
            attempt.attempt_id,
            worker_id=_worker_id(),
            lease_seconds=30,
        )
        await start_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
        )
    return (
        attempt.attempt_id,
        created_step.step_id,
        lease.token,
        lease.generation,
    )


async def _prepare_agent_execution_attempt(
    payload: dict[str, Any],
    *,
    temporal_attempt: int,
) -> tuple[UUID, UUID, str, int]:
    tenant_id = _uuid(payload, "tenant_id")
    principal_id = _uuid(payload, "principal_id")
    workflow_id = _uuid(payload, "workflow_run_id")
    plan_step_key = str(payload["step"]["step_key"])
    step_key = str(payload.get("run_step_key") or plan_step_key)
    step_kind = str(payload.get("run_step_kind") or "agent_model")
    step_index = int(payload["step_index"])
    source_id = _uuid(payload, "source_id")
    async with tenant_transaction(tenant_id, principal_id) as connection:
        row = await _step_row(connection, workflow_id, step_key)
        if row is None:
            raise ApplicationError(
                f"modeling step {step_key} was not created by source generation",
                type="agent_modeling_step_missing",
                non_retryable=True,
            )
        if int(row["step_index"]) != step_index or row["kind"] != step_kind:
            raise ApplicationError(
                f"modeling step {step_key} identity does not match its plan",
                type="agent_modeling_step_conflict",
                non_retryable=True,
            )
        source = (
            await connection.execute(
                text(
                    """
                    SELECT step_run_id, source_hash FROM agent_generated_sources
                    WHERE tenant_id=:tenant_id AND id=:source_id
                    """
                ),
                {"tenant_id": tenant_id, "source_id": source_id},
            )
        ).mappings().one_or_none()
        if source is None or source["step_run_id"] != row["id"]:
            raise ApplicationError(
                "execution source does not belong to the planned modeling step",
                type="agent_source_step_mismatch",
                non_retryable=True,
            )

        step_status = StepStatus(row["status"])
        active = (
            await connection.execute(
                text(
                    """
                    SELECT id, status FROM execution_attempts
                    WHERE workflow_run_id=:workflow_id
                      AND step_run_id=:step_id
                      AND status IN ('pending', 'leased', 'running')
                    ORDER BY attempt_number FOR UPDATE
                    """
                ),
                {"workflow_id": workflow_id, "step_id": row["id"]},
            )
        ).mappings().all()
        for active_attempt in active:
            await transition_attempt(
                connection,
                active_attempt["id"],
                expected=AttemptStatus(active_attempt["status"]),
                target=AttemptStatus.FAILED,
                error_code="superseded_by_temporal_retry",
                error_message="A newer Temporal activity attempt fenced this execution.",
            )
        if active and step_status is StepStatus.RUNNING:
            await transition_step(
                connection,
                row["id"],
                expected=StepStatus.RUNNING,
                target=StepStatus.FAILED,
                error_code="superseded_by_temporal_retry",
            )
            step_status = StepStatus.FAILED
        if step_status in {StepStatus.FAILED, StepStatus.TIMED_OUT}:
            await transition_step(
                connection,
                row["id"],
                expected=step_status,
                target=StepStatus.READY,
            )
            step_status = StepStatus.READY
        if step_status is StepStatus.READY:
            await transition_step(
                connection,
                row["id"],
                expected=StepStatus.READY,
                target=StepStatus.RUNNING,
            )
        elif step_status is not StepStatus.RUNNING:
            raise ApplicationError(
                f"modeling step {step_key} is terminal in {step_status.value}",
                type="agent_modeling_step_terminal",
                non_retryable=True,
            )

        execution_payload = {
            "schema_version": "durable-agent-execution.v1",
            "temporal_activity_id": activity.info().activity_id,
            "temporal_attempt": temporal_attempt,
            "candidate_build_id": str(payload["candidate_build_id"]),
            "source_id": str(source_id),
            "source_hash": source["source_hash"],
            "step": payload["step"],
            "run_step_key": step_key,
            "run_step_kind": step_kind,
            "original_plan_step_key": plan_step_key,
        }
        attempt = await create_attempt(
            connection,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            step_id=row["id"],
            idempotency_key=(
                f"agent-v2:{workflow_id}:{step_key}:attempt:{temporal_attempt}"
            ),
            execution_payload=execution_payload,
        )
        lease = await lease_attempt(
            connection,
            attempt.attempt_id,
            worker_id=_worker_id(),
            lease_seconds=30,
        )
        await start_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
        )
    return attempt.attempt_id, row["id"], lease.token, lease.generation


async def _prepare_agent_validation_attempt(
    payload: dict[str, Any],
    *,
    temporal_attempt: int,
    step_key: str,
    step_index: int,
    step_kind: str,
) -> tuple[UUID, UUID, str, int]:
    tenant_id = _uuid(payload, "tenant_id")
    principal_id = _uuid(payload, "principal_id")
    workflow_id = _uuid(payload, "workflow_run_id")
    candidate_build_id = _uuid(payload, "candidate_build_id")
    manifest_id = _uuid(payload, "staging_manifest_id")
    async with tenant_transaction(tenant_id, principal_id) as connection:
        manifest = (
            await connection.execute(
                text(
                    """
                    SELECT candidate_build_id, workflow_run_id
                    FROM agent_staging_manifests
                    WHERE tenant_id=:tenant_id AND id=:manifest_id
                    """
                ),
                {"tenant_id": tenant_id, "manifest_id": manifest_id},
            )
        ).mappings().one_or_none()
        if manifest is None:
            raise ApplicationError(
                "validation input manifest does not exist",
                type="agent_validation_manifest_missing",
                non_retryable=True,
            )
        if (
            manifest["candidate_build_id"] != candidate_build_id
            or manifest["workflow_run_id"] != workflow_id
        ):
            raise ApplicationError(
                "validation input manifest belongs to another candidate",
                type="agent_validation_manifest_conflict",
                non_retryable=True,
            )
        created = await create_step(
            connection,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            step_key=step_key,
            step_index=step_index,
            kind=step_kind,
        )
        row = await _step_row(connection, workflow_id, step_key)
        status = StepStatus(row["status"])
        active = (
            await connection.execute(
                text(
                    """
                    SELECT id, status FROM execution_attempts
                    WHERE workflow_run_id=:workflow_id
                      AND step_run_id=:step_id
                      AND status IN ('pending', 'leased', 'running')
                    ORDER BY attempt_number FOR UPDATE
                    """
                ),
                {"workflow_id": workflow_id, "step_id": created.step_id},
            )
        ).mappings().all()
        for active_attempt in active:
            await transition_attempt(
                connection,
                active_attempt["id"],
                expected=AttemptStatus(active_attempt["status"]),
                target=AttemptStatus.FAILED,
                error_code="superseded_by_temporal_retry",
                error_message="A newer Temporal activity attempt fenced validation.",
            )
        if active and status is StepStatus.RUNNING:
            await transition_step(
                connection,
                created.step_id,
                expected=StepStatus.RUNNING,
                target=StepStatus.FAILED,
                error_code="superseded_by_temporal_retry",
            )
            status = StepStatus.FAILED
        if status is StepStatus.PENDING:
            await transition_step(
                connection,
                created.step_id,
                expected=StepStatus.PENDING,
                target=StepStatus.READY,
            )
            status = StepStatus.READY
        if status in {StepStatus.FAILED, StepStatus.TIMED_OUT}:
            await transition_step(
                connection,
                created.step_id,
                expected=status,
                target=StepStatus.READY,
            )
            status = StepStatus.READY
        if status is StepStatus.READY:
            await transition_step(
                connection,
                created.step_id,
                expected=StepStatus.READY,
                target=StepStatus.RUNNING,
            )
        elif status is not StepStatus.RUNNING:
            raise ApplicationError(
                f"validation step {step_key} is terminal in {status.value}",
                type="agent_validation_step_terminal",
                non_retryable=True,
            )
        execution_payload = {
            "schema_version": "durable-agent-validation.v1",
            "temporal_activity_id": activity.info().activity_id,
            "temporal_attempt": temporal_attempt,
            "candidate_build_id": str(candidate_build_id),
            "staging_manifest_id": str(manifest_id),
            "gate": step_kind,
        }
        attempt = await create_attempt(
            connection,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            step_id=created.step_id,
            idempotency_key=(
                f"agent-v2:{workflow_id}:{step_key}:attempt:{temporal_attempt}"
            ),
            execution_payload=execution_payload,
        )
        lease = await lease_attempt(
            connection,
            attempt.attempt_id,
            worker_id=_worker_id(),
            lease_seconds=30,
        )
        await start_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
        )
    return attempt.attempt_id, created.step_id, lease.token, lease.generation


async def _stored_validation_attempt_result(
    connection,
    *,
    workflow_id: UUID,
    step_key: str,
) -> dict[str, Any] | None:
    row = (
        await connection.execute(
            text(
                """
                SELECT a.id AS attempt_id, a.step_run_id, a.result_payload
                FROM step_runs s
                JOIN execution_attempts a ON a.step_run_id=s.id
                WHERE s.workflow_run_id=:workflow_id AND s.step_key=:step_key
                  AND s.status='succeeded' AND a.status='succeeded'
                  AND a.result_payload IS NOT NULL
                ORDER BY a.attempt_number DESC LIMIT 1
                """
            ),
            {"workflow_id": workflow_id, "step_key": step_key},
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    return {
        **dict(row["result_payload"]),
        "attempt_id": str(row["attempt_id"]),
        "step_id": str(row["step_run_id"]),
        "replayed": True,
    }


async def _await_provider_operation(operation):
    """Keep long streamed planning calls cancellable and detect dead workers."""
    if not activity.in_activity():
        return await operation

    async def heartbeat():
        while True:
            activity.heartbeat({"stage": "provider_operation"})
            await asyncio.sleep(3)

    beat = asyncio.create_task(heartbeat())
    try:
        return await operation
    finally:
        beat.cancel()
        try:
            await beat
        except asyncio.CancelledError:
            pass


async def _heartbeat_loop(
    *,
    tenant_id: UUID,
    principal_id: UUID,
    attempt_id: UUID,
    lease_token: str,
    lease_generation: int,
) -> None:
    while True:
        activity.heartbeat(
            {
                "execution_attempt_id": str(attempt_id),
                "lease_generation": lease_generation,
            }
        )
        async with tenant_transaction(tenant_id, principal_id) as connection:
            await heartbeat_attempt(
                connection,
                attempt_id,
                lease_token=lease_token,
                lease_generation=lease_generation,
                lease_seconds=30,
            )
        await asyncio.sleep(5)


async def _run_backend_with_heartbeats(
    backend: ExecutionBackend,
    spec: ExecutionSpec,
    *,
    tenant_id: UUID,
    principal_id: UUID,
    attempt_id: UUID,
    lease_token: str,
    lease_generation: int,
    materialized_inputs: dict[str, Path] | None = None,
) -> MaterializedExecutionOutcome:
    execution_task = asyncio.create_task(
        backend.execute(
            spec,
            materialized_inputs=materialized_inputs,
        )
    )
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(
            tenant_id=tenant_id,
            principal_id=principal_id,
            attempt_id=attempt_id,
            lease_token=lease_token,
            lease_generation=lease_generation,
        )
    )
    try:
        done, _ = await asyncio.wait(
            {execution_task, heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if execution_task in done:
            return await execution_task
        if heartbeat_task in done:
            heartbeat_error = heartbeat_task.exception()
            if heartbeat_error is not None:
                execution_task.cancel()
                try:
                    await execution_task
                except (asyncio.CancelledError, Exception):
                    pass
                raise heartbeat_error
        return await execution_task
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        if not execution_task.done():
            execution_task.cancel()
            try:
                await execution_task
            except (asyncio.CancelledError, Exception):
                pass


def interrupted_execution_status(details) -> ExecutionStatus:
    """User cancellation is terminal; an interrupted activity can be retried."""
    if details is None or details.cancel_requested:
        return ExecutionStatus.CANCELLED
    if details.timed_out:
        return ExecutionStatus.TIMED_OUT
    if details.worker_shutdown or details.not_found or details.paused or details.reset:
        return ExecutionStatus.FAILED
    return ExecutionStatus.CANCELLED


async def _mark_execution_failure(
    payload: dict[str, Any],
    *,
    attempt_id: UUID,
    step_id: UUID,
    status: ExecutionStatus,
    error_code: str,
    error_message: str,
    error: ExecutionError | None = None,
) -> None:
    tenant_id = _uuid(payload, "tenant_id")
    principal_id = _uuid(payload, "principal_id")
    if status is ExecutionStatus.CANCELLED and activity.in_activity():
        read_details = getattr(activity, "cancellation_details", None)
        details = read_details() if read_details else None
        status = interrupted_execution_status(details)
        if status is not ExecutionStatus.CANCELLED:
            error_code = "temporal_activity_timed_out" if status is ExecutionStatus.TIMED_OUT else "temporal_activity_interrupted"
            error_message = f"Temporal interrupted this attempt: {details}"
    target_attempt = (
        AttemptStatus.TIMED_OUT
        if status == ExecutionStatus.TIMED_OUT
        else AttemptStatus.CANCELLED
        if status == ExecutionStatus.CANCELLED
        else AttemptStatus.FAILED
    )
    target_step = (
        StepStatus.TIMED_OUT
        if target_attempt == AttemptStatus.TIMED_OUT
        else StepStatus.CANCELLED
        if target_attempt == AttemptStatus.CANCELLED
        else StepStatus.FAILED
    )
    async with tenant_transaction(tenant_id, principal_id) as connection:
        # Match prepare's lock order and fence late cleanup from an old worker.
        # Its cancellation must never terminate the newer running attempt's step.
        step_status = await connection.scalar(
            text("SELECT status FROM step_runs WHERE id=:id FOR UPDATE"),
            {"id": step_id},
        )
        attempt_status = await connection.scalar(
            text("SELECT status FROM execution_attempts WHERE id=:id FOR UPDATE"),
            {"id": attempt_id},
        )
        if attempt_status not in {"pending", "leased", "running"}:
            return
        await transition_attempt(
            connection,
            attempt_id,
            expected=AttemptStatus(attempt_status),
            target=target_attempt,
            error_code=error_code,
            error_message=error_message,
            error=error,
        )
        if step_status == StepStatus.RUNNING.value:
            await transition_step(
                connection,
                step_id,
                expected=StepStatus.RUNNING,
                target=target_step,
                error_code=error_code,
                error_message=error_message,
                error=error,
            )
        change_set_id = payload.get("change_set_id")
        if change_set_id:
            await connection.execute(
                text(
                    """
                    UPDATE change_sets
                    SET validation_summary=CAST(:validation AS jsonb),
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=:change_set_id AND status='pending_review'
                    """
                ),
                {
                    "change_set_id": UUID(str(change_set_id)),
                    "validation": json.dumps(
                        {
                            "status": "failed",
                            "issue_count": 1,
                            "error_code": error_code,
                            "attempt_id": str(attempt_id),
                        },
                        separators=(",", ":"),
                    ),
                },
            )



_MODELING_MEDIA_TYPES = {
    "fcstd": "application/vnd.freecad.fcstd",
    "step": "model/step",
    "stl": "model/stl",
    "dxf": "image/vnd.dxf",
    "svg": "image/svg+xml",
    "png": "image/png",
    "json": "application/json",
    "state": "application/json",
}
