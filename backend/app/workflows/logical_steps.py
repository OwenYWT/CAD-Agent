"""Persisted logical-step lifecycle and lossless result replay."""
from __future__ import annotations
import json
from typing import Any
from uuid import UUID
from sqlalchemy import text
from temporalio.exceptions import ApplicationError
from app.db import tenant_transaction
from app.domain.runs import StepStatus, WorkflowStatus
from app.repositories.runs import append_workflow_event
from app.services.run_state import create_step, transition_step, transition_workflow

async def _step_row(connection, workflow_id: UUID, step_key: str):
    return (
        await connection.execute(
            text(
                """
                SELECT id, status, step_index, kind
                FROM step_runs
                WHERE workflow_run_id=:workflow_id AND step_key=:step_key
                FOR UPDATE
                """
            ),
            {"workflow_id": workflow_id, "step_key": step_key},
        )
    ).mappings().one_or_none()


async def _workflow_status(connection, workflow_id: UUID) -> WorkflowStatus:
    value = await connection.scalar(
        text("SELECT status FROM workflow_runs WHERE id=:id FOR UPDATE"),
        {"id": workflow_id},
    )
    if value is None:
        raise KeyError(workflow_id)
    return WorkflowStatus(value)


async def _succeed_logical_step(
    connection,
    *,
    tenant_id: UUID,
    workflow_id: UUID,
    step_key: str,
    step_index: int,
    kind: str,
) -> UUID:
    created = await create_step(
        connection,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        step_key=step_key,
        step_index=step_index,
        kind=kind,
    )
    row = await _step_row(connection, workflow_id, step_key)
    status = StepStatus(row["status"])
    if status == StepStatus.SUCCEEDED:
        return created.step_id
    if status == StepStatus.PENDING:
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
    if status == StepStatus.READY:
        await transition_step(
            connection,
            created.step_id,
            expected=StepStatus.READY,
            target=StepStatus.RUNNING,
        )
        status = StepStatus.RUNNING
    if status == StepStatus.RUNNING:
        await transition_step(
            connection,
            created.step_id,
            expected=StepStatus.RUNNING,
            target=StepStatus.SUCCEEDED,
        )
    return created.step_id


async def _stored_agent_step_result(
    connection,
    *,
    workflow_id: UUID,
    event_type: str,
    step_key: str,
) -> dict[str, Any] | None:
    row = (
        await connection.execute(
            text(
                """
                SELECT payload FROM task_events
                WHERE workflow_run_id=:workflow_id
                  AND event_type=:event_type
                  AND payload->>'step_key'=:step_key
                ORDER BY sequence DESC
                LIMIT 1
                """
            ),
            {
                "workflow_id": workflow_id,
                "event_type": event_type,
                "step_key": step_key,
            },
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    stored = dict(row["payload"])
    # The event projection remains queryable JSONB. Its execution result also
    # keeps the original JSON text, so a worker restart cannot change numbers.
    result = json.loads(stored["_result_json"]) if "_result_json" in stored else stored
    if isinstance(result.get("base_state"), dict):
        from app.freecad.reference_geometry import normalize_reference_state
        result = {**result, "base_state": normalize_reference_state(result["base_state"])}
    return {**result, "replayed": True}


async def _start_agent_logical_step(
    connection,
    *,
    tenant_id: UUID,
    workflow_id: UUID,
    step_key: str,
    step_index: int | None,
    kind: str,
) -> UUID:
    status = await _workflow_status(connection, workflow_id)
    if status == WorkflowStatus.PENDING:
        await transition_workflow(
            connection,
            workflow_id,
            expected=WorkflowStatus.PENDING,
            target=WorkflowStatus.PLANNING,
        )
    elif status not in {WorkflowStatus.PLANNING, WorkflowStatus.RUNNING}:
        raise ApplicationError(
            f"workflow cannot plan in {status.value}",
            type="workflow_state_conflict",
            non_retryable=True,
        )

    created = await create_step(
        connection,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        step_key=step_key,
        step_index=step_index,
        kind=kind,
    )
    row = await _step_row(connection, workflow_id, step_key)
    step_status = StepStatus(row["status"])
    if step_status == StepStatus.SUCCEEDED:
        raise ApplicationError(
            f"successful step {step_key} has no stored result",
            type="agent_step_result_missing",
            non_retryable=True,
        )
    if step_status in {StepStatus.FAILED, StepStatus.TIMED_OUT}:
        await transition_step(
            connection,
            created.step_id,
            expected=step_status,
            target=StepStatus.READY,
        )
        step_status = StepStatus.READY
    if step_status == StepStatus.PENDING:
        await transition_step(
            connection,
            created.step_id,
            expected=StepStatus.PENDING,
            target=StepStatus.READY,
        )
        step_status = StepStatus.READY
    if step_status == StepStatus.READY:
        await transition_step(
            connection,
            created.step_id,
            expected=StepStatus.READY,
            target=StepStatus.RUNNING,
        )
    return created.step_id


async def _complete_agent_logical_step(
    connection,
    *,
    tenant_id: UUID,
    workflow_id: UUID,
    step_id: UUID,
    event_type: str,
    result: dict[str, Any],
    enter_running: bool = False,
) -> dict[str, Any]:
    step_status = await connection.scalar(
        text("SELECT status FROM step_runs WHERE id=:id FOR UPDATE"),
        {"id": step_id},
    )
    if step_status == StepStatus.RUNNING.value:
        await transition_step(
            connection,
            step_id,
            expected=StepStatus.RUNNING,
            target=StepStatus.SUCCEEDED,
        )
    elif step_status != StepStatus.SUCCEEDED.value:
        raise ApplicationError(
            f"agent planning step cannot complete in {step_status}",
            type="workflow_state_conflict",
            non_retryable=True,
        )
    await append_workflow_event(
        connection,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        event_type=event_type,
        payload={**result, "_result_json": json.dumps(result, allow_nan=False)},
    )
    if enter_running:
        workflow_status = await _workflow_status(connection, workflow_id)
        if workflow_status == WorkflowStatus.PLANNING:
            await transition_workflow(
                connection,
                workflow_id,
                expected=WorkflowStatus.PLANNING,
                target=WorkflowStatus.RUNNING,
            )
    return result


async def _fail_agent_logical_step(
    *,
    tenant_id: UUID,
    principal_id: UUID,
    workflow_id: UUID,
    step_key: str,
    error_code: str,
    error_message: str,
) -> None:
    async with tenant_transaction(tenant_id, principal_id) as connection:
        row = await _step_row(connection, workflow_id, step_key)
        if row is None:
            return
        status = StepStatus(row["status"])
        if status == StepStatus.RUNNING:
            await transition_step(
                connection,
                row["id"],
                expected=StepStatus.RUNNING,
                target=StepStatus.FAILED,
                error_code=error_code[:200],
                error_message=error_message[:4000],
            )
