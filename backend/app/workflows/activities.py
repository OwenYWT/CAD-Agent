"""Temporal activities containing every MCAD side effect."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent.durable_plan import AgentPlan
from app.agent.durable_planner import DurableAgentPlanner
from app.agent.orchestrator import Orchestrator
from app.api.error_messages import public_generation_error
from app.db import tenant_transaction
from app.dfm.models import StepAnalysisResult
from app.dfm.step_analyzer_script import STEP_ANALYSIS_SCRIPT
from app.domain.runs import AttemptStatus, StepStatus, WorkflowStatus
from app.execution.backend import ExecutionBackend, MaterializedExecutionOutcome
from app.execution.composition import get_execution_backend
from app.execution.contracts import (
    ArtifactInput,
    ExecutionSource,
    ExecutionSpec,
    ExecutionStatus,
    OutputDeclaration,
    ResourceLimits,
    RuntimeRequirement,
)
from app.object_store import get_object, put_file, sha256_object
from app.repositories.revisions import (
    StaleBaseRevision,
    create_candidate_change_set,
)
from app.repositories.agent_candidates import (
    create_agent_candidate_build,
    transition_agent_candidate_build,
)
from app.domain.revisions import CandidateBuildStatus
from app.repositories.runs import append_workflow_event
from app.services.artifact_commit import (
    authorize_artifact_upload,
    commit_artifacts,
)
from app.services.change_sets import (
    accept_change_set,
    commit_change_set,
    reject_change_set,
    update_change_set_evidence,
)
from app.services.design_analysis import build_design_analysis_response
from app.services.run_state import (
    create_attempt,
    create_step,
    heartbeat_attempt,
    lease_attempt,
    request_workflow_cancellation,
    start_attempt,
    transition_attempt,
    transition_step,
    transition_workflow,
)
from app.validation.dfm_analyzer import DFMAnalyzer
from app.llm import is_nonretryable_provider_error
from app.models.schemas import CADPlan, ModificationPlan
from app.workflows.source_preparation import SourcePreparer
from app.workflows.temporal import (
    McadAgentWorkflowV2Request,
    McadSourcePreparationRequest,
)


def _uuid(payload: dict[str, Any], key: str) -> UUID:
    return UUID(str(payload[key]))


def _agent_v2_request(payload: dict[str, Any]) -> McadAgentWorkflowV2Request:
    fields = McadAgentWorkflowV2Request.model_fields
    return McadAgentWorkflowV2Request.model_validate(
        {key: payload[key] for key in fields if key in payload}
    )


def _worker_id() -> str:
    return f"temporal:{socket.gethostname()}:{os.getpid()}"


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
    return {**dict(row["payload"]), "replayed": True}


async def _start_agent_logical_step(
    connection,
    *,
    tenant_id: UUID,
    workflow_id: UUID,
    step_key: str,
    step_index: int,
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
        payload=result,
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


async def _mark_execution_failure(
    payload: dict[str, Any],
    *,
    attempt_id: UUID,
    step_id: UUID,
    status: ExecutionStatus,
    error_code: str,
    error_message: str,
) -> None:
    tenant_id = _uuid(payload, "tenant_id")
    principal_id = _uuid(payload, "principal_id")
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
        attempt_status = await connection.scalar(
            text("SELECT status FROM execution_attempts WHERE id=:id FOR UPDATE"),
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


class McadWorkflowActivities:
    def __init__(
        self,
        backend: ExecutionBackend | None = None,
        *,
        source_preparer: SourcePreparer | None = None,
        durable_planner: DurableAgentPlanner | None = None,
    ):
        self.backend = backend or get_execution_backend()
        self.source_preparer = source_preparer or SourcePreparer(
            Orchestrator(execution_backend=self.backend)
        )
        self.durable_planner = durable_planner or DurableAgentPlanner()

    @staticmethod
    def _agent_planning_error(exc: Exception) -> ApplicationError:
        public = public_generation_error(exc)
        non_retryable = (
            is_nonretryable_provider_error(exc)
            or isinstance(exc, (RuntimeError, ValueError, KeyError))
        )
        return ApplicationError(
            public["message"],
            type=public["type"][:200],
            non_retryable=non_retryable,
        )

    @activity.defn(name="agent_v2.requirements")
    async def agent_requirements(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request = _agent_v2_request(payload)
        tenant_id = request.tenant_id
        principal_id = request.principal_id
        workflow_id = request.workflow_run_id
        step_key = "agent-requirements"
        event_type = "agent.requirements.completed"
        async with tenant_transaction(tenant_id, principal_id) as connection:
            replay = await _stored_agent_step_result(
                connection,
                workflow_id=workflow_id,
                event_type=event_type,
                step_key=step_key,
            )
            if replay is not None:
                return replay
            step_id = await _start_agent_logical_step(
                connection,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                step_key=step_key,
                step_index=0,
                kind="agent_requirements",
            )
        try:
            if request.operation == "generate":
                requirements = await self.durable_planner.requirements_generation(
                    request.objective
                )
            else:
                requirements = await self.durable_planner.requirements_modification(
                    request.existing_code or "",
                    request.objective,
                )
            result = {
                "step_key": step_key,
                "operation": request.operation,
                "requirements": requirements.model_dump(mode="json"),
            }
        except Exception as exc:
            error = self._agent_planning_error(exc)
            await _fail_agent_logical_step(
                tenant_id=tenant_id,
                principal_id=principal_id,
                workflow_id=workflow_id,
                step_key=step_key,
                error_code=error.type or "agent_requirements_failed",
                error_message=str(error),
            )
            raise error from exc
        async with tenant_transaction(tenant_id, principal_id) as connection:
            return await _complete_agent_logical_step(
                connection,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                step_id=step_id,
                event_type=event_type,
                result=result,
            )

    @activity.defn(name="agent_v2.decompose")
    async def agent_decompose(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request = _agent_v2_request(payload)
        if request.operation != "generate":
            raise ApplicationError(
                "Only generation plans can be decomposed.",
                type="invalid_agent_decomposition",
                non_retryable=True,
            )
        tenant_id = request.tenant_id
        principal_id = request.principal_id
        workflow_id = request.workflow_run_id
        step_key = "agent-decompose"
        event_type = "agent.decomposition.completed"
        async with tenant_transaction(tenant_id, principal_id) as connection:
            replay = await _stored_agent_step_result(
                connection,
                workflow_id=workflow_id,
                event_type=event_type,
                step_key=step_key,
            )
            if replay is not None:
                return replay
            step_id = await _start_agent_logical_step(
                connection,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                step_key=step_key,
                step_index=1,
                kind="agent_decompose",
            )
        try:
            plan = CADPlan.model_validate(payload["requirements"])
            decomposition = await self.durable_planner.decompose_generation(plan)
            result = {
                "step_key": step_key,
                "decomposition": decomposition,
                "skipped": decomposition is None,
            }
        except Exception as exc:
            error = self._agent_planning_error(exc)
            await _fail_agent_logical_step(
                tenant_id=tenant_id,
                principal_id=principal_id,
                workflow_id=workflow_id,
                step_key=step_key,
                error_code=error.type or "agent_decomposition_failed",
                error_message=str(error),
            )
            raise error from exc
        async with tenant_transaction(tenant_id, principal_id) as connection:
            return await _complete_agent_logical_step(
                connection,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                step_id=step_id,
                event_type=event_type,
                result=result,
            )

    @activity.defn(name="agent_v2.plan")
    async def agent_plan(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request = _agent_v2_request(payload)
        tenant_id = request.tenant_id
        principal_id = request.principal_id
        workflow_id = request.workflow_run_id
        step_key = "agent-plan"
        event_type = "agent.plan.completed"
        step_index = 2 if request.operation == "generate" else 1
        async with tenant_transaction(tenant_id, principal_id) as connection:
            replay = await _stored_agent_step_result(
                connection,
                workflow_id=workflow_id,
                event_type=event_type,
                step_key=step_key,
            )
            if replay is not None:
                AgentPlan.model_validate(replay["plan"])
                return replay
            step_id = await _start_agent_logical_step(
                connection,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                step_key=step_key,
                step_index=step_index,
                kind="agent_plan",
            )
        try:
            if request.operation == "generate":
                requirements = CADPlan.model_validate(payload["requirements"])
                plan = self.durable_planner.compose_generation(
                    request.objective,
                    requirements,
                    decomposition=payload.get("decomposition"),
                    output_formats=request.output_formats,
                )
            else:
                requirements = ModificationPlan.model_validate(
                    payload["requirements"]
                )
                plan = self.durable_planner.compose_modification(
                    request.objective,
                    requirements,
                    expected_base_revision_id=request.expected_base_revision_id,
                    output_formats=request.output_formats,
                )
            result = {
                "step_key": step_key,
                "plan": plan.temporal_payload(),
                "requires_confirmation": (
                    plan.confirmation_policy.value == "required"
                ),
                "confirmation_reason": plan.confirmation_reason,
            }
        except Exception as exc:
            error = self._agent_planning_error(exc)
            await _fail_agent_logical_step(
                tenant_id=tenant_id,
                principal_id=principal_id,
                workflow_id=workflow_id,
                step_key=step_key,
                error_code=error.type or "agent_plan_failed",
                error_message=str(error),
            )
            raise error from exc
        async with tenant_transaction(tenant_id, principal_id) as connection:
            return await _complete_agent_logical_step(
                connection,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                step_id=step_id,
                event_type=event_type,
                result=result,
                enter_running=True,
            )

    @activity.defn(name="agent_v2.allocate_candidate")
    async def agent_allocate_candidate(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request = _agent_v2_request(payload)
        plan = AgentPlan.model_validate(payload["plan"])
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            created = await create_agent_candidate_build(
                connection,
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                branch_id=request.branch_id,
                base_revision_id=request.expected_base_revision_id,
                workflow_id=request.workflow_run_id,
                created_by_principal_id=request.principal_id,
                plan=plan.temporal_payload(),
            )
        return {
            "candidate_build_id": str(created.candidate_build_id),
            "status": created.status.value,
            "replayed": created.replayed,
        }

    @activity.defn(name="agent_v2.terminate_candidate")
    async def agent_terminate_candidate(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request = _agent_v2_request(payload)
        target = CandidateBuildStatus(str(payload["target_status"]))
        if target not in {
            CandidateBuildStatus.FAILED,
            CandidateBuildStatus.CANCELLED,
            CandidateBuildStatus.ABANDONED,
        }:
            raise ApplicationError(
                "unsupported candidate terminal status",
                type="invalid_candidate_terminal_status",
                non_retryable=True,
            )
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            result = await transition_agent_candidate_build(
                connection,
                tenant_id=request.tenant_id,
                candidate_build_id=_uuid(payload, "candidate_build_id"),
                expected=CandidateBuildStatus.BUILDING,
                target=target,
                failure_code=str(payload.get("error_code") or "") or None,
                failure_message=(
                    str(payload.get("error_message") or "")[:4000] or None
                ),
            )
        return {
            "candidate_build_id": str(result.candidate_build_id),
            "status": result.status.value,
            "replayed": result.replayed,
        }

    @activity.defn(name="mcad.prepare_source")
    async def prepare_source(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        tenant_id = _uuid(payload, "tenant_id")
        principal_id = _uuid(payload, "principal_id")
        workflow_id = _uuid(payload, "workflow_run_id")
        step_key = str(
            payload.get("preparation_step_key") or "prepare_source"
        )
        preparation = McadSourcePreparationRequest.model_validate(
            payload["preparation"]
        )

        async with tenant_transaction(
            tenant_id,
            principal_id,
        ) as connection:
            replay = (
                await connection.execute(
                    text(
                        """
                        SELECT payload FROM task_events
                        WHERE workflow_run_id=:workflow_id
                          AND event_type='source.prepared'
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
            if replay is not None:
                return {**dict(replay["payload"]), "replayed": True}

            status = await _workflow_status(connection, workflow_id)
            if status == WorkflowStatus.PENDING:
                await transition_workflow(
                    connection,
                    workflow_id,
                    expected=WorkflowStatus.PENDING,
                    target=WorkflowStatus.PLANNING,
                )
            elif status not in {
                WorkflowStatus.PLANNING,
                WorkflowStatus.RUNNING,
            }:
                raise ApplicationError(
                    f"workflow cannot prepare source in {status.value}",
                    type="workflow_state_conflict",
                    non_retryable=True,
                )

            created = await create_step(
                connection,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                step_key=step_key,
                step_index=int(payload.get("preparation_step_index", 0)),
                kind=f"source_{preparation.operation}",
            )
            row = await _step_row(connection, workflow_id, step_key)
            step_status = StepStatus(row["status"])
            if step_status in {
                StepStatus.FAILED,
                StepStatus.TIMED_OUT,
            }:
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
            elif step_status != StepStatus.RUNNING:
                raise ApplicationError(
                    f"source step is terminal in {step_status.value}",
                    type="source_step_terminal",
                    non_retryable=True,
                )
            await append_workflow_event(
                connection,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                event_type="source.preparation_started",
                payload={
                    "step_key": step_key,
                    "operation": preparation.operation,
                },
            )

        try:
            prepared = await self.source_preparer.prepare(preparation)
        except asyncio.CancelledError:
            async with tenant_transaction(
                tenant_id,
                principal_id,
            ) as connection:
                row = await _step_row(connection, workflow_id, step_key)
                if row and row["status"] == StepStatus.RUNNING.value:
                    await transition_step(
                        connection,
                        row["id"],
                        expected=StepStatus.RUNNING,
                        target=StepStatus.CANCELLED,
                        error_code="source_preparation_cancelled",
                        error_message="MCAD source preparation was cancelled.",
                    )
            raise
        except Exception as exc:
            public_error = public_generation_error(exc)
            provider_failure = public_error["type"].startswith("Provider")
            error_code = (
                public_error["type"]
                if provider_failure
                else "source_preparation_failed"
            )
            error_message = (
                public_error["message"]
                if provider_failure
                else str(exc)[:4000]
            )
            async with tenant_transaction(
                tenant_id,
                principal_id,
            ) as connection:
                row = await _step_row(connection, workflow_id, step_key)
                if row and row["status"] == StepStatus.RUNNING.value:
                    await transition_step(
                        connection,
                        row["id"],
                        expected=StepStatus.RUNNING,
                        target=StepStatus.FAILED,
                        error_code=error_code,
                        error_message=error_message,
                    )
            raise ApplicationError(
                error_message,
                {"cause": type(exc).__name__},
                type=error_code,
                non_retryable=(
                    isinstance(exc, (RuntimeError, ValueError))
                    or is_nonretryable_provider_error(exc)
                ),
            ) from exc

        event_payload = {
            **prepared,
            "step_key": step_key,
            "operation": preparation.operation,
        }
        async with tenant_transaction(
            tenant_id,
            principal_id,
        ) as connection:
            row = await _step_row(connection, workflow_id, step_key)
            if row and row["status"] == StepStatus.RUNNING.value:
                await transition_step(
                    connection,
                    row["id"],
                    expected=StepStatus.RUNNING,
                    target=StepStatus.SUCCEEDED,
                )
            await append_workflow_event(
                connection,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                event_type="source.prepared",
                payload=event_payload,
            )
        return event_payload

    @activity.defn(name="mcad.plan")
    async def plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        tenant_id = _uuid(payload, "tenant_id")
        principal_id = _uuid(payload, "principal_id")
        project_id = _uuid(payload, "project_id")
        workflow_id = _uuid(payload, "workflow_run_id")
        branch_id = _uuid(payload, "branch_id")
        base_revision_id = _uuid(payload, "expected_base_revision_id")
        executions = [payload["primary"]]
        if payload.get("followup"):
            executions.append(payload["followup"])
        manifest = {
            "schema_version": "mcad-revision-manifest.v1",
            "objective": payload["objective"],
            "base_revision_id": str(base_revision_id),
            "executions": [
                {
                    "step_key": item["step_key"],
                    "operation": item["operation"],
                    "mode": item["mode"],
                    "source_sha256": hashlib.sha256(
                        item["source_code"].encode("utf-8")
                    ).hexdigest(),
                    "source_code": item["source_code"],
                    "outputs": item["outputs"],
                }
                for item in executions
            ],
        }
        try:
            async with tenant_transaction(tenant_id, principal_id) as connection:
                status = await _workflow_status(connection, workflow_id)
                if status == WorkflowStatus.PENDING:
                    await transition_workflow(
                        connection,
                        workflow_id,
                        expected=WorkflowStatus.PENDING,
                        target=WorkflowStatus.PLANNING,
                    )
                elif status not in {
                    WorkflowStatus.PLANNING,
                    WorkflowStatus.RUNNING,
                }:
                    raise ApplicationError(
                        f"workflow cannot plan in {status.value}",
                        type="workflow_terminal",
                        non_retryable=True,
                    )
                await _succeed_logical_step(
                    connection,
                    tenant_id=tenant_id,
                    workflow_id=workflow_id,
                    step_key="plan",
                    step_index=int(payload.get("plan_step_index", 0)),
                    kind="mcad_plan",
                )
                candidate = await create_candidate_change_set(
                    connection,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    branch_id=branch_id,
                    expected_base_revision_id=base_revision_id,
                    created_by_principal_id=principal_id,
                    idempotency_key=f"workflow:{workflow_id}:candidate",
                    objective=str(payload["objective"]),
                    candidate_manifest=manifest,
                    change_summary={
                        "operation_count": len(executions),
                        "operations": [item["operation"] for item in executions],
                    },
                    source_workflow_run_id=workflow_id,
                )
                if not candidate.replayed:
                    await append_workflow_event(
                        connection,
                        tenant_id=tenant_id,
                        workflow_id=workflow_id,
                        event_type="workflow.plan_recorded",
                        payload={
                            "change_set_id": str(candidate.change_set_id),
                            "candidate_revision_id": str(
                                candidate.candidate_revision_id
                            ),
                            "steps": [item["step_key"] for item in executions],
                        },
                    )
                status = await _workflow_status(connection, workflow_id)
                if status == WorkflowStatus.PLANNING:
                    await transition_workflow(
                        connection,
                        workflow_id,
                        expected=WorkflowStatus.PLANNING,
                        target=WorkflowStatus.RUNNING,
                    )
            return {
                "change_set_id": str(candidate.change_set_id),
                "candidate_revision_id": str(candidate.candidate_revision_id),
                "revision_number": candidate.revision_number,
                "replayed": candidate.replayed,
            }
        except ApplicationError:
            raise
        except (
            KeyError,
            PermissionError,
            StaleBaseRevision,
            ValueError,
        ) as exc:
            raise ApplicationError(
                str(exc),
                type="invalid_mcad_plan",
                non_retryable=True,
            ) from exc

    @activity.defn(name="mcad.execute")
    async def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        info = activity.info()
        tenant_id = _uuid(payload, "tenant_id")
        principal_id = _uuid(payload, "principal_id")
        project_id = _uuid(payload, "project_id")
        workflow_id = _uuid(payload, "workflow_run_id")
        revision_id = _uuid(payload, "revision_id")
        execution = dict(payload["execution"])

        async with tenant_transaction(tenant_id, principal_id) as connection:
            replay = await _stored_execution_result(
                connection,
                workflow_id=workflow_id,
                step_key=str(execution["step_key"]),
            )
        if replay is not None:
            return replay

        attempt_id, step_id, lease_token, lease_generation = (
            await _prepare_execution_attempt(
                payload,
                temporal_attempt=info.attempt,
            )
        )
        outcome: MaterializedExecutionOutcome | None = None
        try:
            snapshot = await asyncio.to_thread(self.backend.runtime_snapshot)
            source_code = str(execution["source_code"])
            spec = ExecutionSpec(
                execution_attempt_id=str(attempt_id),
                workflow_run_id=str(workflow_id),
                step_run_id=str(step_id),
                tenant_id=str(tenant_id),
                project_id=str(project_id),
                expected_base_revision_id=str(
                    payload["expected_base_revision_id"]
                ),
                idempotency_key=(
                    f"temporal:{workflow_id}:{execution['step_key']}:{info.attempt}"
                ),
                capability=str(execution["capability"]),
                operation=str(execution["operation"]),
                mode=str(execution["mode"]),
                source=ExecutionSource(
                    language=str(execution["source_language"]),
                    code=source_code,
                    sha256=hashlib.sha256(
                        source_code.encode("utf-8")
                    ).hexdigest(),
                ),
                outputs=tuple(
                    OutputDeclaration.model_validate(item)
                    for item in execution["outputs"]
                ),
                runtime=RuntimeRequirement(
                    image_digest=snapshot.image_digest,
                    platform=snapshot.platform,
                    sandbox_tier="ephemeral-job",
                ),
                limits=ResourceLimits(
                    timeout_seconds=int(execution["timeout_seconds"])
                ),
                metadata={
                    "revision_id": str(revision_id),
                    "temporal_activity_id": info.activity_id,
                    "temporal_attempt": info.attempt,
                },
            )
            outcome = await _run_backend_with_heartbeats(
                self.backend,
                spec,
                tenant_id=tenant_id,
                principal_id=principal_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                lease_generation=lease_generation,
            )
            async with tenant_transaction(
                tenant_id,
                principal_id,
            ) as connection:
                cancellation_requested = await connection.scalar(
                    text(
                        """
                        SELECT cancellation_requested_at IS NOT NULL
                        FROM workflow_runs WHERE id=:id
                        """
                    ),
                    {"id": workflow_id},
                )
            if activity.is_cancelled() or cancellation_requested:
                await _mark_execution_failure(
                    payload,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    status=ExecutionStatus.CANCELLED,
                    error_code="execution_cancelled",
                    error_message="MCAD execution was cancelled.",
                )
                raise asyncio.CancelledError
            if outcome.result.status != ExecutionStatus.SUCCEEDED:
                error = outcome.result.error
                code = error.code if error else "execution_failed"
                message = error.message if error else "MCAD execution failed"
                await _mark_execution_failure(
                    payload,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    status=outcome.result.status,
                    error_code=code,
                    error_message=message,
                )
                retryable = bool(
                    error
                    and error.category.value
                    in {"infrastructure", "timeout", "resource"}
                )
                raise ApplicationError(
                    message,
                    {
                        "execution_attempt_id": str(attempt_id),
                        "category": (
                            error.category.value if error else "internal"
                        ),
                    },
                    type=code,
                    non_retryable=not retryable,
                )

            upload_ids: list[UUID] = []
            for output_name, path in outcome.files.items():
                declared = next(
                    item
                    for item in outcome.result.outputs
                    if item.name == path.name
                )
                filename = (
                    f"{execution['step_key']}-{Path(declared.name).name}"
                )
                authorization = await authorize_artifact_upload(
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    project_id=project_id,
                    revision_id=revision_id,
                    attempt_id=attempt_id,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                    filename=filename,
                    artifact_kind=str(output_name).lower(),
                    content_type=declared.media_type,
                    declared_size_bytes=declared.size_bytes,
                    declared_sha256=declared.sha256,
                )
                await put_file(
                    authorization.staging_object_key,
                    path,
                    content_type=declared.media_type,
                )
                upload_ids.append(authorization.upload_id)

            committed = await commit_artifacts(
                tenant_id=tenant_id,
                principal_id=principal_id,
                attempt_id=attempt_id,
                revision_id=revision_id,
                upload_ids=upload_ids,
                lease_token=lease_token,
                lease_generation=lease_generation,
                runtime_metadata=(
                    outcome.result.provenance.model_dump(mode="json")
                    if outcome.result.provenance
                    else {}
                ),
            )
            async with tenant_transaction(tenant_id, principal_id) as connection:
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
            return {
                "status": "succeeded",
                "attempt_id": str(attempt_id),
                "revision_id": str(revision_id),
                "artifacts": [
                    {
                        "artifact_id": str(item.artifact_id),
                        "filename": item.filename,
                        "artifact_kind": item.artifact_kind,
                        "object_key": item.object_key,
                        "size_bytes": item.size_bytes,
                        "sha256": item.sha256,
                        "content_type": item.content_type,
                    }
                    for item in committed.artifacts
                ],
                "metrics": dict(outcome.result.metrics),
                "replayed": committed.replayed,
            }
        except ApplicationError:
            raise
        except asyncio.CancelledError:
            await _mark_execution_failure(
                payload,
                attempt_id=attempt_id,
                step_id=step_id,
                status=ExecutionStatus.CANCELLED,
                error_code="execution_cancelled",
                error_message="Temporal cancelled the MCAD activity.",
            )
            raise
        except Exception as exc:
            await _mark_execution_failure(
                payload,
                attempt_id=attempt_id,
                step_id=step_id,
                status=ExecutionStatus.FAILED,
                error_code="execution_activity_failed",
                error_message=str(exc)[:4000],
            )
            raise ApplicationError(
                "MCAD execution activity failed before a result was committed.",
                {
                    "execution_attempt_id": str(attempt_id),
                    "cause": type(exc).__name__,
                },
                type="execution_activity_failed",
                non_retryable=False,
            ) from exc
        finally:
            if outcome and outcome.work_dir:
                await asyncio.to_thread(
                    shutil.rmtree,
                    outcome.work_dir,
                    True,
                )

    @activity.defn(name="mcad.check")
    async def check(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one durable engineering check against immutable CAD artifacts."""
        info = activity.info()
        tenant_id = _uuid(payload, "tenant_id")
        principal_id = _uuid(payload, "principal_id")
        project_id = _uuid(payload, "project_id")
        workflow_id = _uuid(payload, "workflow_run_id")
        source_workflow_id = _uuid(payload, "source_workflow_run_id")
        source_revision_id = _uuid(payload, "source_revision_id")

        async with tenant_transaction(
            tenant_id,
            principal_id,
        ) as connection:
            revision_source = await connection.scalar(
                text(
                    """
                    SELECT source_workflow_run_id
                    FROM project_revisions
                    WHERE tenant_id=:tenant_id
                      AND project_id=:project_id
                      AND id=:revision_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "revision_id": source_revision_id,
                },
            )
            if revision_source is None:
                raise ApplicationError(
                    "The source revision does not exist in this project.",
                    type="check_source_revision_missing",
                    non_retryable=True,
                )
            if revision_source != source_workflow_id:
                raise ApplicationError(
                    "The source workflow does not own the source revision.",
                    type="check_source_revision_mismatch",
                    non_retryable=True,
                )

            persisted_report = (
                await connection.execute(
                    text(
                        """
                        SELECT id AS artifact_id, filename, artifact_kind,
                               object_key, size_bytes, sha256, content_type
                        FROM artifacts
                        WHERE tenant_id=:tenant_id
                          AND project_id=:project_id
                          AND workflow_run_id=:workflow_id
                          AND revision_id=:revision_id
                          AND artifact_kind='dfm_report'
                        ORDER BY created_at DESC, id DESC
                        LIMIT 1
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "project_id": project_id,
                        "workflow_id": workflow_id,
                        "revision_id": source_revision_id,
                    },
                )
            ).mappings().one_or_none()

        if persisted_report is not None:
            report_bytes = await get_object(persisted_report["object_key"])
            if (
                len(report_bytes) != int(persisted_report["size_bytes"])
                or hashlib.sha256(report_bytes).hexdigest()
                != persisted_report["sha256"]
            ):
                raise ApplicationError(
                    "The persisted engineering-check report failed integrity verification.",
                    type="check_report_integrity_failed",
                    non_retryable=True,
                )
            report_document = json.loads(report_bytes)
            analysis = dict(report_document["analysis"])
            return await _complete_check_workflow(
                payload,
                report_artifact={
                    **dict(persisted_report),
                    "artifact_id": str(
                        persisted_report["artifact_id"]
                    ),
                },
                analysis=analysis,
                replayed=True,
            )

        async with tenant_transaction(
            tenant_id,
            principal_id,
        ) as connection:
            source_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT id AS artifact_id, artifact_kind, filename,
                               content_type, size_bytes, sha256, object_key
                        FROM artifacts
                        WHERE tenant_id=:tenant_id
                          AND project_id=:project_id
                          AND workflow_run_id=:source_workflow_id
                          AND revision_id=:revision_id
                          AND artifact_kind IN ('step', 'stl')
                        ORDER BY created_at DESC, id DESC
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "project_id": project_id,
                        "source_workflow_id": source_workflow_id,
                        "revision_id": source_revision_id,
                    },
                )
            ).mappings().all()
            status = await _workflow_status(connection, workflow_id)
            if status == WorkflowStatus.PENDING:
                await transition_workflow(
                    connection,
                    workflow_id,
                    expected=WorkflowStatus.PENDING,
                    target=WorkflowStatus.PLANNING,
                )
                status = WorkflowStatus.PLANNING
            if status == WorkflowStatus.PLANNING:
                await transition_workflow(
                    connection,
                    workflow_id,
                    expected=WorkflowStatus.PLANNING,
                    target=WorkflowStatus.RUNNING,
                )
            elif status not in {
                WorkflowStatus.RUNNING,
                WorkflowStatus.SUCCEEDED,
            }:
                raise ApplicationError(
                    f"check workflow cannot run in {status.value}",
                    type="check_workflow_state_conflict",
                    non_retryable=True,
                )

        source_artifacts: dict[str, dict[str, Any]] = {}
        for row in source_rows:
            kind = str(row["artifact_kind"]).lower()
            source_artifacts.setdefault(kind, dict(row))
        if "stl" not in source_artifacts:
            raise ApplicationError(
                "The source revision has no immutable STL artifact.",
                type="check_source_stl_missing",
                non_retryable=True,
            )

        source_code = (
            STEP_ANALYSIS_SCRIPT
            if "step" in source_artifacts
            else "result = {'error': 'No STEP artifact is available.'}"
        )
        execution = {
            "step_key": "engineering_check",
            "kind": "dfm_check",
            "capability": "mcad.local",
            "operation": "analyze",
            "mode": "analysis",
            "source_language": "python",
            "source_code": source_code,
            "outputs": [
                {
                    "name": "json",
                    "media_type": "application/json",
                    "required": True,
                    "max_size_bytes": 16 * 1024 * 1024,
                }
            ],
            "timeout_seconds": int(payload.get("timeout_seconds", 120)),
        }
        attempt_payload = {
            **payload,
            "revision_id": str(source_revision_id),
            "step_index": 0,
            "execution": execution,
        }
        attempt_id, step_id, lease_token, lease_generation = (
            await _prepare_execution_attempt(
                attempt_payload,
                temporal_attempt=info.attempt,
            )
        )
        if step_id.int == 0:
            raise ApplicationError(
                "A completed engineering check is missing its report artifact.",
                type="check_report_missing",
                non_retryable=True,
            )

        temp_dir = Path(tempfile.mkdtemp(prefix="cad_check_"))
        outcome: MaterializedExecutionOutcome | None = None
        report_committed = False
        post_heartbeat: asyncio.Task | None = None
        try:
            materialized: dict[str, Path] = {}
            for kind, row in source_artifacts.items():
                data = await get_object(row["object_key"])
                if (
                    len(data) != int(row["size_bytes"])
                    or hashlib.sha256(data).hexdigest() != row["sha256"]
                ):
                    raise ApplicationError(
                        f"The source {kind.upper()} artifact failed integrity verification.",
                        type="check_source_artifact_integrity_failed",
                        non_retryable=True,
                    )
                path = temp_dir / f"source.{kind}"
                path.write_bytes(data)
                row["local_path"] = path
                if kind == "step":
                    materialized[str(row["artifact_id"])] = path

            snapshot = await asyncio.to_thread(
                self.backend.runtime_snapshot
            )
            input_declarations: tuple[ArtifactInput, ...] = ()
            if "step" in source_artifacts:
                step_artifact = source_artifacts["step"]
                input_declarations = (
                    ArtifactInput(
                        artifact_id=str(step_artifact["artifact_id"]),
                        filename="model.step",
                        sha256=str(step_artifact["sha256"]),
                        size_bytes=int(step_artifact["size_bytes"]),
                        media_type=str(step_artifact["content_type"]),
                    ),
                )
            spec = ExecutionSpec(
                execution_attempt_id=str(attempt_id),
                workflow_run_id=str(workflow_id),
                step_run_id=str(step_id),
                tenant_id=str(tenant_id),
                project_id=str(project_id),
                expected_base_revision_id=str(source_revision_id),
                idempotency_key=(
                    f"temporal:{workflow_id}:engineering_check:{info.attempt}"
                ),
                capability="mcad.local",
                operation="analyze",
                mode="analysis",
                source=ExecutionSource(
                    language="python",
                    code=source_code,
                    sha256=hashlib.sha256(
                        source_code.encode("utf-8")
                    ).hexdigest(),
                ),
                inputs=input_declarations,
                outputs=(
                    OutputDeclaration(
                        name="json",
                        media_type="application/json",
                        max_size_bytes=16 * 1024 * 1024,
                    ),
                ),
                runtime=RuntimeRequirement(
                    image_digest=snapshot.image_digest,
                    platform=snapshot.platform,
                    sandbox_tier="ephemeral-job",
                ),
                limits=ResourceLimits(
                    timeout_seconds=int(
                        payload.get("timeout_seconds", 120)
                    )
                ),
                metadata={
                    "revision_id": str(source_revision_id),
                    "source_workflow_run_id": str(source_workflow_id),
                    "temporal_activity_id": info.activity_id,
                    "temporal_attempt": info.attempt,
                },
            )
            outcome = await _run_backend_with_heartbeats(
                self.backend,
                spec,
                tenant_id=tenant_id,
                principal_id=principal_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                lease_generation=lease_generation,
                materialized_inputs=materialized,
            )

            if activity.is_cancelled():
                raise asyncio.CancelledError
            async with tenant_transaction(
                tenant_id,
                principal_id,
            ) as connection:
                cancellation_requested = await connection.scalar(
                    text(
                        """
                        SELECT cancellation_requested_at IS NOT NULL
                        FROM workflow_runs WHERE id=:id
                        """
                    ),
                    {"id": workflow_id},
                )
            if cancellation_requested:
                raise asyncio.CancelledError

            step_analysis_error: str | None = None
            step_data: StepAnalysisResult | None = None
            if outcome.result.status == ExecutionStatus.SUCCEEDED:
                analysis_path = outcome.files.get("json")
                if analysis_path is None:
                    step_analysis_error = (
                        "The isolated runtime returned no STEP analysis JSON."
                    )
                else:
                    try:
                        step_data = StepAnalysisResult.model_validate_json(
                            analysis_path.read_text(encoding="utf-8")
                        )
                        step_analysis_error = step_data.error
                    except Exception as exc:
                        step_analysis_error = (
                            "The isolated STEP analysis result was invalid: "
                            f"{type(exc).__name__}"
                        )
            else:
                error = outcome.result.error
                step_analysis_error = (
                    error.message
                    if error
                    else "The isolated STEP analysis failed."
                )

            post_heartbeat = asyncio.create_task(
                _heartbeat_loop(
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                    attempt_id=attempt_id,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                )
            )
            analyzer = DFMAnalyzer()
            design_analysis = await analyzer.analyze(
                stl_path=source_artifacts["stl"]["local_path"],
                code=str(payload.get("code") or ""),
                description=str(payload.get("description") or ""),
                process=payload.get("process"),
                material=payload.get("material"),
                precomputed_step_data=step_data,
            )
            response = build_design_analysis_response(design_analysis)
            analysis = response.model_dump(mode="json")
            report_document = {
                "schema_version": "dfm-report.v1",
                "workflow_run_id": str(workflow_id),
                "source_workflow_run_id": str(source_workflow_id),
                "source_revision_id": str(source_revision_id),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "step_analysis_error": step_analysis_error,
                "analysis": analysis,
            }
            report_path = temp_dir / (
                f"engineering-check-{workflow_id}.json"
            )
            report_path.write_text(
                json.dumps(
                    report_document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            report_bytes = report_path.read_bytes()
            report_sha = hashlib.sha256(report_bytes).hexdigest()
            authorization = await authorize_artifact_upload(
                tenant_id=tenant_id,
                principal_id=principal_id,
                project_id=project_id,
                revision_id=source_revision_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                lease_generation=lease_generation,
                filename=report_path.name,
                artifact_kind="dfm_report",
                content_type="application/json",
                declared_size_bytes=len(report_bytes),
                declared_sha256=report_sha,
            )
            await put_file(
                authorization.staging_object_key,
                report_path,
                content_type="application/json",
            )
            if post_heartbeat.done():
                heartbeat_error = post_heartbeat.exception()
                if heartbeat_error is not None:
                    raise heartbeat_error
            else:
                post_heartbeat.cancel()
                try:
                    await post_heartbeat
                except asyncio.CancelledError:
                    pass
            post_heartbeat = None
            runtime_metadata = {
                "check_engine": "deterministic-dfm.v1",
                "source_artifacts": {
                    kind: {
                        "artifact_id": str(row["artifact_id"]),
                        "sha256": str(row["sha256"]),
                    }
                    for kind, row in source_artifacts.items()
                },
                "isolated_step_analysis": {
                    "status": outcome.result.status.value,
                    "error": step_analysis_error,
                    "provenance": (
                        outcome.result.provenance.model_dump(mode="json")
                        if outcome.result.provenance
                        else None
                    ),
                },
            }
            committed = await commit_artifacts(
                tenant_id=tenant_id,
                principal_id=principal_id,
                attempt_id=attempt_id,
                revision_id=source_revision_id,
                upload_ids=[authorization.upload_id],
                lease_token=lease_token,
                lease_generation=lease_generation,
                runtime_metadata=runtime_metadata,
            )
            artifact = committed.artifacts[0]
            report_artifact = {
                "artifact_id": str(artifact.artifact_id),
                "filename": artifact.filename,
                "artifact_kind": artifact.artifact_kind,
                "object_key": artifact.object_key,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
                "content_type": artifact.content_type,
            }
            report_committed = True
            return await _complete_check_workflow(
                payload,
                report_artifact=report_artifact,
                analysis=analysis,
                replayed=committed.replayed,
            )
        except asyncio.CancelledError:
            if not report_committed:
                await _mark_execution_failure(
                    attempt_payload,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    status=ExecutionStatus.CANCELLED,
                    error_code="check_cancelled",
                    error_message="The engineering check was cancelled.",
                )
            raise
        except ApplicationError:
            if not report_committed:
                await _mark_execution_failure(
                    attempt_payload,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    status=ExecutionStatus.FAILED,
                    error_code="check_activity_failed",
                    error_message="The engineering check did not commit a report.",
                )
            raise
        except Exception as exc:
            if not report_committed:
                await _mark_execution_failure(
                    attempt_payload,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    status=ExecutionStatus.FAILED,
                    error_code="check_activity_failed",
                    error_message=str(exc)[:4000],
                )
            raise ApplicationError(
                "Engineering check failed before a report was committed.",
                {
                    "execution_attempt_id": str(attempt_id),
                    "cause": type(exc).__name__,
                },
                type="check_activity_failed",
                non_retryable=False,
            ) from exc
        finally:
            if post_heartbeat is not None:
                post_heartbeat.cancel()
                try:
                    await post_heartbeat
                except asyncio.CancelledError:
                    pass
            if outcome and outcome.work_dir:
                await asyncio.to_thread(
                    shutil.rmtree,
                    outcome.work_dir,
                    True,
                )
            await asyncio.to_thread(shutil.rmtree, temp_dir, True)

    @activity.defn(name="mcad.validate")
    async def validate(self, payload: dict[str, Any]) -> dict[str, Any]:
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

    @activity.defn(name="mcad.wait_confirmation")
    async def wait_confirmation(self, payload: dict[str, Any]) -> dict[str, Any]:
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

    @activity.defn(name="mcad.resume_after_confirmation")
    async def resume_after_confirmation(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
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

    @activity.defn(name="mcad.finalize")
    async def finalize(self, payload: dict[str, Any]) -> dict[str, Any]:
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

    @activity.defn(name="mcad.record_cancel")
    async def record_cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
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

    @activity.defn(name="mcad.record_timeout")
    async def record_timeout(self, payload: dict[str, Any]) -> dict[str, Any]:
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

    @activity.defn(name="mcad.record_failure")
    async def record_failure(self, payload: dict[str, Any]) -> dict[str, Any]:
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

    def registered(self) -> list:
        return [
            self.prepare_source,
            self.plan,
            self.execute,
            self.check,
            self.validate,
            self.wait_confirmation,
            self.resume_after_confirmation,
            self.finalize,
            self.record_cancel,
            self.record_timeout,
            self.record_failure,
        ]

    def registered_agent_v2(self) -> list:
        """Activities available only to the version-isolated Agent queue."""
        return [
            self.agent_requirements,
            self.agent_decompose,
            self.agent_plan,
            self.agent_allocate_candidate,
            self.agent_terminate_candidate,
            self.wait_confirmation,
            self.resume_after_confirmation,
            self.record_cancel,
            self.record_timeout,
            self.record_failure,
        ]
