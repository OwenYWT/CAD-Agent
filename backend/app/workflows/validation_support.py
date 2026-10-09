"""Validation support shared by narrow activity handlers."""
from __future__ import annotations
from typing import Any
from uuid import UUID
from sqlalchemy import text
from temporalio.exceptions import ApplicationError
from app.db import tenant_transaction
from app.domain.runs import AttemptStatus, StepStatus, WorkflowStatus
from app.execution.contracts import ExecutionStatus
from app.repositories.agent_candidates import record_validation_evidence
from app.repositories.runs import append_workflow_event
from app.services.run_state import complete_attempt, transition_attempt, transition_step, transition_workflow
from app.workflows.logical_steps import _step_row, _workflow_status
from app.workflows.inputs import _uuid
from app.workflows.constraint_evidence import persist_repair_contract, persist_profile_contract

async def _record_agent_validation_outcome(
    payload: dict[str, Any],
    *,
    candidate_build_id: UUID,
    staging_manifest_id: UUID,
    gate: str,
    mode: str,
    outcome: str,
    evidence: dict[str, Any],
    attempt_id: UUID,
    step_id: UUID,
    execution_status: ExecutionStatus,
    lease_token: str,
    lease_generation: int,
    result_payload: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
):
    """Commit a validation attempt, step, and evidence atomically."""
    tenant_id = _uuid(payload, "tenant_id")
    principal_id = _uuid(payload, "principal_id")
    workflow_id = _uuid(payload, "workflow_run_id")
    async with tenant_transaction(tenant_id, principal_id) as connection:
        if execution_status is ExecutionStatus.SUCCEEDED:
            if result_payload is None:
                raise ValueError("successful validation requires result_payload")
            await complete_attempt(
                connection,
                attempt_id,
                lease_token=lease_token,
                lease_generation=lease_generation,
                result_payload=result_payload,
            )
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
                    f"validation step is terminal in {step_status}",
                    type="agent_validation_step_terminal",
                    non_retryable=True,
                )
        else:
            target_attempt = (
                AttemptStatus.TIMED_OUT
                if execution_status is ExecutionStatus.TIMED_OUT
                else AttemptStatus.CANCELLED
                if execution_status is ExecutionStatus.CANCELLED
                else AttemptStatus.FAILED
            )
            target_step = (
                StepStatus.TIMED_OUT
                if target_attempt is AttemptStatus.TIMED_OUT
                else StepStatus.CANCELLED
                if target_attempt is AttemptStatus.CANCELLED
                else StepStatus.FAILED
            )
            attempt_status = await connection.scalar(
                text(
                    "SELECT status FROM execution_attempts "
                    "WHERE id=:id FOR UPDATE"
                ),
                {"id": attempt_id},
            )
            if attempt_status in {"pending", "leased", "running"}:
                await transition_attempt(
                    connection,
                    attempt_id,
                    expected=AttemptStatus(attempt_status),
                    target=target_attempt,
                    error_code=error_code,
                    error_message=error_message,
                )
            elif attempt_status != target_attempt.value:
                raise ApplicationError(
                    f"validation attempt is terminal in {attempt_status}",
                    type="agent_validation_attempt_terminal",
                    non_retryable=True,
                )
            step_status = await connection.scalar(
                text("SELECT status FROM step_runs WHERE id=:id FOR UPDATE"),
                {"id": step_id},
            )
            if step_status == StepStatus.RUNNING.value:
                await transition_step(
                    connection,
                    step_id,
                    expected=StepStatus.RUNNING,
                    target=target_step,
                    error_code=error_code,
                    error_message=error_message,
                )
            elif step_status != target_step.value:
                raise ApplicationError(
                    f"validation step is terminal in {step_status}",
                    type="agent_validation_step_terminal",
                    non_retryable=True,
                )
        return await record_validation_evidence(
            connection,
            tenant_id=tenant_id,
            candidate_build_id=candidate_build_id,
            workflow_id=workflow_id,
            step_id=step_id,
            attempt_id=attempt_id,
            staging_manifest_id=staging_manifest_id,
            gate=gate,
            mode=mode,
            outcome=outcome,
            evidence=evidence,
        )


async def _complete_check_workflow(
    payload: dict[str, Any],
    *,
    report_artifact: dict[str, Any],
    analysis: dict[str, Any],
    replayed: bool,
) -> dict[str, Any]:
    tenant_id = _uuid(payload, "tenant_id")
    principal_id = _uuid(payload, "principal_id")
    workflow_id = _uuid(payload, "workflow_run_id")
    violation_severities = {
        str(item.get("severity") or "").lower()
        for item in analysis.get("rule_violations") or []
    }
    validation_status = (
        "indeterminate" if analysis.get("evaluation_status") == "indeterminate" else
        "failed"
        if "critical" in violation_severities
        else "warning"
        if "warning" in violation_severities
        else "passed"
    )
    async with tenant_transaction(tenant_id, principal_id) as connection:
        row = await _step_row(
            connection,
            workflow_id,
            "engineering_check",
        )
        if row is None:
            raise ApplicationError(
                "Engineering check has no persisted StepRun.",
                type="check_step_missing",
                non_retryable=True,
            )
        step_status = StepStatus(row["status"])
        if step_status in {StepStatus.FAILED, StepStatus.TIMED_OUT}:
            await transition_step(
                connection,
                row["id"],
                expected=step_status,
                target=StepStatus.READY,
            )
            step_status = StepStatus.READY
        if step_status == StepStatus.READY:
            await transition_step(
                connection,
                row["id"],
                expected=StepStatus.READY,
                target=StepStatus.RUNNING,
            )
            step_status = StepStatus.RUNNING
        if step_status == StepStatus.RUNNING:
            await transition_step(
                connection,
                row["id"],
                expected=StepStatus.RUNNING,
                target=StepStatus.SUCCEEDED,
            )
        event_exists = await connection.scalar(
            text(
                """
                SELECT 1 FROM task_events
                WHERE workflow_run_id=:workflow_id
                  AND event_type='validation.completed'
                  AND payload->>'report_artifact_id'=:artifact_id
                """
            ),
            {
                "workflow_id": workflow_id,
                "artifact_id": str(report_artifact["artifact_id"]),
            },
        )
        if event_exists is None:
            await append_workflow_event(
                connection,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                event_type="validation.completed",
                payload={
                    "step_key": "engineering_check",
                    "scope": "geometry_and_dfm",
                    "status": validation_status,
                    "design_score": analysis.get("design_score"),
                    "issue_count": len(
                        analysis.get("rule_violations") or []
                    ),
                    "report_artifact_id": str(
                        report_artifact["artifact_id"]
                    ),
                    "report_sha256": report_artifact["sha256"],
                },
            )
        workflow_status = await _workflow_status(connection, workflow_id)
        if workflow_status == WorkflowStatus.PENDING:
            await transition_workflow(
                connection,
                workflow_id,
                expected=WorkflowStatus.PENDING,
                target=WorkflowStatus.PLANNING,
            )
            workflow_status = WorkflowStatus.PLANNING
        if workflow_status == WorkflowStatus.PLANNING:
            await transition_workflow(
                connection,
                workflow_id,
                expected=WorkflowStatus.PLANNING,
                target=WorkflowStatus.RUNNING,
            )
            workflow_status = WorkflowStatus.RUNNING
        if workflow_status == WorkflowStatus.RUNNING:
            await transition_workflow(
                connection,
                workflow_id,
                expected=WorkflowStatus.RUNNING,
                target=WorkflowStatus.SUCCEEDED,
            )
        elif workflow_status != WorkflowStatus.SUCCEEDED:
            raise ApplicationError(
                f"check workflow cannot complete in {workflow_status.value}",
                type="check_workflow_state_conflict",
                non_retryable=True,
            )
    return {
        "status": "succeeded",
        "workflow_run_id": str(workflow_id),
        "analysis": analysis,
        "report_artifact": report_artifact,
        "replayed": replayed,
    }


async def _record_freecad_inspections(connection, request, step_key, provenance, *, source_id=None, source_hash=None):
    if provenance.get('profile_replan'):
        await persist_profile_contract(connection, request, source_id=source_id,
            source_hash=source_hash, contract=provenance['profile_replan'])
    if provenance.get('constraint_repair'):
        await persist_repair_contract(connection, request, source_id=source_id,
            source_hash=source_hash, contract=provenance['constraint_repair'])
    for index, call in enumerate(provenance.get("tool_calls", [])):
        await append_workflow_event(connection, tenant_id=request.tenant_id,
            workflow_id=request.workflow_run_id, event_type="agent.freecad.tool_read",
            payload={"step_key": step_key, "index": index, **call})
    if provenance.get("execution_tool_call"):
        import hashlib
        call = provenance["execution_tool_call"]
        await append_workflow_event(connection, tenant_id=request.tenant_id,
            workflow_id=request.workflow_run_id, event_type="agent.freecad.tool_dispatched",
            payload={"step_key": step_key, "tool_call_id": call["id"],
                "tool_name": call["function"]["name"], "source_id": str(source_id),
                "source_hash": source_hash, "status": "pending_execution",
                "provider_response_id": provenance.get("provider_response_id"),
                "arguments_sha256": hashlib.sha256(call["function"]["arguments"].encode()).hexdigest()})
    if provenance.get('engineering_evidence'):
        await append_workflow_event(connection, tenant_id=request.tenant_id, workflow_id=request.workflow_run_id,
            event_type='agent.engineering_evidence.used', payload={'step_key':step_key,
                'references':provenance['engineering_evidence'], 'provider':provenance.get('provider'),
                'model':provenance.get('model'), 'request_hash':provenance.get('request_hash')})
    for index, inspection in enumerate(provenance.get("inspection_calls", [])):
        await append_workflow_event(connection, tenant_id=request.tenant_id,
            workflow_id=request.workflow_run_id, event_type="agent.kernel_inspected",
            payload={"step_key": step_key, "inspection_index": index, **inspection})


async def record_freecad_rejection(request, step_key: str, evidence: dict) -> None:
    """Persist rejected provider output privately; events carry only integrity metadata."""
    import hashlib
    import json
    from app.object_store import put_object
    payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode('utf-8')
    digest = hashlib.sha256(payload).hexdigest()
    key = f"tenants/{request.tenant_id}/workflow-evidence/{request.workflow_run_id}/{digest}.json"
    stored = await put_object(key, payload, content_type='application/json')
    async with tenant_transaction(request.tenant_id, request.principal_id) as connection:
        await append_workflow_event(connection, tenant_id=request.tenant_id,
            workflow_id=request.workflow_run_id, event_type='agent.freecad.plan_rejected',
            payload={'step_key':step_key,'error_type':evidence['error_type'],
                'attempt':evidence['attempt'], 'evidence_object_key':stored['key'],
                'sha256':stored['sha256'],'size_bytes':stored['size_bytes']})
