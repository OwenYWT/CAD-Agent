"""Temporal activities containing every MCAD side effect."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent.orchestrator import Orchestrator
from app.api.error_messages import public_generation_error
from app.db import tenant_transaction
from app.domain.runs import AttemptStatus, StepStatus, WorkflowStatus
from app.execution.backend import ExecutionBackend, MaterializedExecutionOutcome
from app.execution.composition import get_execution_backend
from app.execution.contracts import (
    ExecutionSource,
    ExecutionSpec,
    ExecutionStatus,
    OutputDeclaration,
    ResourceLimits,
    RuntimeRequirement,
)
from app.object_store import put_file, sha256_object
from app.repositories.revisions import (
    StaleBaseRevision,
    create_candidate_change_set,
)
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
from app.llm import is_nonretryable_provider_error
from app.workflows.source_preparation import SourcePreparer
from app.workflows.temporal import McadSourcePreparationRequest


def _uuid(payload: dict[str, Any], key: str) -> UUID:
    return UUID(str(payload[key]))


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
) -> MaterializedExecutionOutcome:
    execution_task = asyncio.create_task(backend.execute(spec))
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


class McadWorkflowActivities:
    def __init__(
        self,
        backend: ExecutionBackend | None = None,
        *,
        source_preparer: SourcePreparer | None = None,
    ):
        self.backend = backend or get_execution_backend()
        self.source_preparer = source_preparer or SourcePreparer(
            Orchestrator(execution_backend=self.backend)
        )

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
            self.validate,
            self.wait_confirmation,
            self.resume_after_confirmation,
            self.finalize,
            self.record_cancel,
            self.record_timeout,
            self.record_failure,
        ]
