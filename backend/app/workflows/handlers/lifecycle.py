"""Lifecycle use cases; Temporal names live in the adapter."""
from __future__ import annotations
from typing import Any
from uuid import UUID
from sqlalchemy import text
from temporalio.exceptions import ApplicationError
from app.db import tenant_transaction
from app.domain.runs import WorkflowStatus
from app.services.change_sets import reject_change_set
from app.services.run_state import request_workflow_cancellation, transition_workflow
from app.workflows.logical_steps import _workflow_status
from app.workflows.inputs import _uuid

async def record_cancel(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _uuid(payload, "tenant_id")
    principal_id = _uuid(payload, "principal_id")
    workflow_id = _uuid(payload, "workflow_run_id")
    reason = str(payload.get("reason") or "cancelled")[:4000]
    change_set_id = payload.get("change_set_id")
    if change_set_id:
        async with tenant_transaction(
            tenant_id,
            principal_id,
        ) as connection:
            change_status = await connection.scalar(
                text("SELECT status FROM change_sets WHERE id=:id"),
                {"id": UUID(str(change_set_id))},
            )
        if change_status == "pending_review":
            await reject_change_set(
                tenant_id=tenant_id,
                reviewer_principal_id=principal_id,
                change_set_id=UUID(str(change_set_id)),
                review_note=reason or "cancelled",
            )
    async with tenant_transaction(tenant_id, principal_id) as connection:
        status = await _workflow_status(connection, workflow_id)
        if status in {
            WorkflowStatus.SUCCEEDED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
            WorkflowStatus.TIMED_OUT,
        }:
            return {"status": status.value, "replayed": True}
        if status != WorkflowStatus.CANCELLING:
            await request_workflow_cancellation(connection, workflow_id)
            status = await _workflow_status(connection, workflow_id)
        if status == WorkflowStatus.CANCELLING:
            await transition_workflow(
                connection,
                workflow_id,
                expected=WorkflowStatus.CANCELLING,
                target=WorkflowStatus.CANCELLED,
            )
    return {"status": "cancelled", "reason": reason}


async def record_timeout(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _uuid(payload, "tenant_id")
    principal_id = _uuid(payload, "principal_id")
    workflow_id = _uuid(payload, "workflow_run_id")
    change_set_id = payload.get("change_set_id")
    if change_set_id:
        async with tenant_transaction(
            tenant_id,
            principal_id,
        ) as connection:
            change_status = await connection.scalar(
                text("SELECT status FROM change_sets WHERE id=:id"),
                {"id": UUID(str(change_set_id))},
            )
        if change_status == "pending_review":
            await reject_change_set(
                tenant_id=tenant_id,
                reviewer_principal_id=principal_id,
                change_set_id=UUID(str(change_set_id)),
                review_note="确认等待超时",
            )
    async with tenant_transaction(tenant_id, principal_id) as connection:
        status = await _workflow_status(connection, workflow_id)
        if status == WorkflowStatus.WAITING_CONFIRMATION:
            await transition_workflow(
                connection,
                workflow_id,
                expected=WorkflowStatus.WAITING_CONFIRMATION,
                target=WorkflowStatus.TIMED_OUT,
                error_code="confirmation_timeout",
                error_message="The confirmation timer expired.",
            )
        elif status != WorkflowStatus.TIMED_OUT:
            raise ApplicationError(
                f"workflow cannot time out in {status.value}",
                type="workflow_state_conflict",
                non_retryable=True,
            )
    return {"status": "timed_out"}


async def record_failure(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _uuid(payload, "tenant_id")
    principal_id = _uuid(payload, "principal_id")
    workflow_id = _uuid(payload, "workflow_run_id")
    message = str(payload.get("error_message") or "workflow failed")[:4000]
    error_code = str(
        payload.get("error_code") or "temporal_workflow_failed"
    )[:200]
    async with tenant_transaction(tenant_id, principal_id) as connection:
        status = await _workflow_status(connection, workflow_id)
        if status in {
            WorkflowStatus.PENDING,
            WorkflowStatus.PLANNING,
            WorkflowStatus.RUNNING,
            WorkflowStatus.WAITING_CONFIRMATION,
            WorkflowStatus.CANCELLING,
        }:
            await transition_workflow(
                connection,
                workflow_id,
                expected=status,
                target=WorkflowStatus.FAILED,
                error_code=error_code,
                error_message=message,
            )
        elif status not in {
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
            WorkflowStatus.TIMED_OUT,
            WorkflowStatus.SUCCEEDED,
        }:
            raise ApplicationError(
                f"workflow cannot fail in {status.value}",
                type="workflow_state_conflict",
                non_retryable=True,
            )
    return {
        "status": "failed",
        "error_code": error_code,
        "error_message": message,
    }


async def acquire_document(payload: dict) -> dict:
    from app.services.cloud_documents import acquire_operation
    return await acquire_operation(UUID(payload["tenant_id"]), UUID(payload["principal_id"]), UUID(payload["workflow_run_id"]))


async def release_document(payload: dict) -> dict:
    from app.services.cloud_documents import finish_operation
    return await finish_operation(UUID(payload["tenant_id"]), UUID(payload["principal_id"]), UUID(payload["workflow_run_id"]))
