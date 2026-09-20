"""Review use cases; Temporal names live in the adapter."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import text
from temporalio.exceptions import ApplicationError
from app.db import tenant_transaction
from app.domain.runs import StepStatus, WorkflowStatus
from app.object_store import sha256_object
from app.repositories.runs import append_workflow_event
from app.services.change_sets import accept_change_set, commit_change_set, update_change_set_evidence
from app.services.run_state import create_step, transition_step, transition_workflow
from app.workflows.logical_steps import _step_row, _workflow_status
from app.workflows.inputs import _uuid

async def validate(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _uuid(payload, "tenant_id")
    principal_id = _uuid(payload, "principal_id")
    workflow_id = _uuid(payload, "workflow_run_id")
    revision_id = _uuid(payload, "revision_id")
    change_set_id = _uuid(payload, "change_set_id")
    step_key = str(payload["step_key"])
    async with tenant_transaction(tenant_id, principal_id) as connection:
        created = await create_step(
            connection,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            step_key=step_key,
            step_index=int(payload["step_index"]),
            kind="artifact_validation",
        )
        row = await _step_row(connection, workflow_id, step_key)
        step_status = StepStatus(row["status"])
        if step_status == StepStatus.SUCCEEDED:
            replay = (
                await connection.execute(
                    text(
                        """
                        SELECT payload FROM task_events
                        WHERE workflow_run_id=:workflow_id
                          AND event_type='validation.completed'
                          AND payload->>'step_key'=:step_key
                        ORDER BY sequence DESC
                        LIMIT 1
                        """
                    ),
                    {
                        "workflow_id": workflow_id,
                        "step_key": step_key,
                    },
                )
            ).mappings().one_or_none()
            if replay is None:
                raise ApplicationError(
                    "Succeeded validation step has no durable result event.",
                    type="validation_result_missing",
                    non_retryable=True,
                )
            return {**dict(replay["payload"]), "replayed": True}
        if step_status == StepStatus.PENDING:
            await transition_step(
                connection,
                created.step_id,
                expected=StepStatus.PENDING,
                target=StepStatus.READY,
            )
            step_status = StepStatus.READY
        if step_status in {StepStatus.FAILED, StepStatus.TIMED_OUT}:
            await transition_step(
                connection,
                created.step_id,
                expected=step_status,
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
        elif step_status not in {
            StepStatus.RUNNING,
            StepStatus.SUCCEEDED,
        }:
            raise ApplicationError(
                f"validation step is terminal in {step_status.value}",
                type="validation_step_terminal",
                non_retryable=True,
            )
        artifacts = (
            await connection.execute(
                text(
                    """
                    SELECT id, filename, object_key, size_bytes, sha256,
                           content_type
                    FROM artifacts
                    WHERE revision_id=:revision_id
                    ORDER BY filename
                    """
                ),
                {"revision_id": revision_id},
            )
        ).mappings().all()
    if not artifacts:
        async with tenant_transaction(
            tenant_id,
            principal_id,
        ) as connection:
            await transition_step(
                connection,
                created.step_id,
                expected=StepStatus.RUNNING,
                target=StepStatus.FAILED,
                error_code="artifact_evidence_missing",
                error_message="No committed artifacts exist for validation.",
            )
        raise ApplicationError(
            "No committed artifacts exist for validation.",
            type="artifact_evidence_missing",
            non_retryable=True,
        )
    checked: list[dict[str, Any]] = []
    for row in artifacts:
        actual = await sha256_object(row["object_key"])
        if (
            actual["size_bytes"] != row["size_bytes"]
            or actual["sha256"] != row["sha256"]
        ):
            async with tenant_transaction(
                tenant_id,
                principal_id,
            ) as connection:
                await transition_step(
                    connection,
                    created.step_id,
                    expected=StepStatus.RUNNING,
                    target=StepStatus.FAILED,
                    error_code="artifact_integrity_failed",
                    error_message=(
                        f"Artifact integrity failed: {row['filename']}"
                    ),
                )
            raise ApplicationError(
                f"Artifact integrity failed: {row['filename']}",
                type="artifact_integrity_failed",
                non_retryable=True,
            )
        checked.append(
            {
                "artifact_id": str(row["id"]),
                "filename": row["filename"],
                "size_bytes": row["size_bytes"],
                "sha256": row["sha256"],
            }
        )
    summary = {
        "status": "passed",
        "issue_count": 0,
        "scope": "artifact_integrity",
        "artifact_count": len(checked),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    await update_change_set_evidence(
        tenant_id=tenant_id,
        principal_id=principal_id,
        change_set_id=change_set_id,
        validation_summary=summary,
        risk_summary={
            "status": "not_assessed",
            "note": (
                "Geometry and DFM are outside this artifact-integrity "
                "validation scope."
            ),
        },
    )
    async with tenant_transaction(tenant_id, principal_id) as connection:
        step_status = await connection.scalar(
            text("SELECT status FROM step_runs WHERE id=:id FOR UPDATE"),
            {"id": created.step_id},
        )
        if step_status == StepStatus.RUNNING.value:
            await transition_step(
                connection,
                created.step_id,
                expected=StepStatus.RUNNING,
                target=StepStatus.SUCCEEDED,
            )
        await append_workflow_event(
            connection,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            event_type="validation.completed",
            payload={
                **summary,
                "step_key": step_key,
                "artifacts": checked,
            },
        )
    return {
        **summary,
        "step_key": step_key,
        "artifacts": checked,
    }


async def wait_confirmation(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _uuid(payload, "tenant_id")
    principal_id = _uuid(payload, "principal_id")
    workflow_id = _uuid(payload, "workflow_run_id")
    async with tenant_transaction(tenant_id, principal_id) as connection:
        status = await _workflow_status(connection, workflow_id)
        if status == WorkflowStatus.RUNNING:
            await transition_workflow(
                connection,
                workflow_id,
                expected=WorkflowStatus.RUNNING,
                target=WorkflowStatus.WAITING_CONFIRMATION,
            )
        elif status != WorkflowStatus.WAITING_CONFIRMATION:
            raise ApplicationError(
                f"workflow cannot wait in {status.value}",
                type="workflow_state_conflict",
                non_retryable=True,
            )
    return {"status": "waiting_confirmation"}


async def resume_after_confirmation(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _uuid(payload, "tenant_id")
    principal_id = _uuid(payload, "principal_id")
    workflow_id = _uuid(payload, "workflow_run_id")
    async with tenant_transaction(tenant_id, principal_id) as connection:
        status = await _workflow_status(connection, workflow_id)
        if status == WorkflowStatus.WAITING_CONFIRMATION:
            await transition_workflow(
                connection,
                workflow_id,
                expected=WorkflowStatus.WAITING_CONFIRMATION,
                target=WorkflowStatus.RUNNING,
            )
        elif status != WorkflowStatus.RUNNING:
            raise ApplicationError(
                f"workflow cannot resume in {status.value}",
                type="workflow_state_conflict",
                non_retryable=True,
            )
        await append_workflow_event(
            connection,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            event_type="workflow.confirmed",
            payload={"note": str(payload.get("note") or "")[:4000]},
        )
    return {"status": "running"}


async def finalize(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _uuid(payload, "tenant_id")
    principal_id = _uuid(payload, "principal_id")
    workflow_id = _uuid(payload, "workflow_run_id")
    change_set_id = _uuid(payload, "change_set_id")
    if bool(payload.get("commit_after_confirmation", True)):
        if not bool(payload.get("require_confirmation", True)):
            raise ApplicationError(
                "Branch advancement requires explicit confirmation.",
                type="confirmation_required",
                non_retryable=True,
            )
        await accept_change_set(
            tenant_id=tenant_id,
            reviewer_principal_id=principal_id,
            change_set_id=change_set_id,
            review_note="Temporal confirmation accepted",
        )
        committed = await commit_change_set(
            tenant_id=tenant_id,
            reviewer_principal_id=principal_id,
            change_set_id=change_set_id,
        )
        return {
            "status": "succeeded",
            "change_set_status": committed.status,
            "committed": True,
        }
    async with tenant_transaction(tenant_id, principal_id) as connection:
        status = await _workflow_status(connection, workflow_id)
        if status == WorkflowStatus.RUNNING:
            await transition_workflow(
                connection,
                workflow_id,
                expected=WorkflowStatus.RUNNING,
                target=WorkflowStatus.SUCCEEDED,
            )
        elif status != WorkflowStatus.SUCCEEDED:
            raise ApplicationError(
                f"workflow cannot finalize in {status.value}",
                type="workflow_state_conflict",
                non_retryable=True,
            )
    return {
        "status": "succeeded",
        "change_set_status": "pending_review",
        "committed": False,
    }
