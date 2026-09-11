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

from app.agent.durable_plan import AgentPlan, normalize_agent_plan_backend, enforce_design_validation
from app.agent.durable_plan import AgentPlanStep
from app.agent.durable_planner import DurableAgentPlanner
from app.agent.durable_repair import (
    DurableRepairSourceGenerator,
    decide_repair,
)
from app.agent.orchestrator import Orchestrator
from app.api.error_messages import public_generation_error
from app.db import tenant_transaction
from app.dfm.models import StepAnalysisResult
from app.dfm.step_analyzer_script import STEP_ANALYSIS_SCRIPT
from app.domain.runs import AttemptStatus, StepStatus, WorkflowStatus
from app.execution.backend import ExecutionBackend, MaterializedExecutionOutcome
from app.execution.canonical import canonical_sha256
from app.execution.composition import get_execution_backend
from app.execution.contracts import (
    ArtifactInput,
    ExecutionError,
    ExecutionSource,
    ExecutionSpec,
    ExecutionStatus,
    OutputDeclaration,
    ResourceLimits,
    RuntimeRequirement,
)
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.bom_contracts import (
    FreeCADBOMDocumentV1,
    FreeCADBOMRequestV1,
)
from app.freecad.state_contract import (
    ParameterStateError,
    compile_parameter_operation_plan,
)
from app.freecad.operation_generator import FreeCADOperationGenerator
from app.object_store import download_object, get_object, put_file, sha256_object
from app.repositories.artifacts import committed_artifact_for_revision
from app.repositories.revisions import (
    StaleBaseRevision,
    create_candidate_change_set,
)
from app.repositories.agent_candidates import (
    accept_staging_manifest,
    create_agent_candidate_build,
    get_generated_source_for_step,
    get_staging_manifest_for_step,
    get_validation_evidence_for_manifest,
    record_generated_source,
    record_validation_evidence,
    transition_agent_candidate_build,
)
from app.domain.revisions import CandidateBuildStatus
from app.repositories.runs import append_workflow_event
from app.services.artifact_commit import (
    authorize_artifact_upload,
    commit_artifacts,
    seal_agent_candidate,
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
    complete_attempt,
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
from app.validation.durable_geometry import (
    DurableGeometryReport,
    indeterminate_geometry_report,
)
from app.validation.durable_visual import (
    DurableVisualValidator,
    VisualRenderEvidence,
    indeterminate_visual_report,
)
from app.validation.durable_dfm import (
    DurableDFMReport,
    indeterminate_dfm_report,
)
from app.validation.dfm_policy_snapshot import resolve_dfm_policy_snapshot
from app.llm import is_nonretryable_provider_error
from app.models.schemas import CADPlan, ModificationPlan
from app.workflows.source_preparation import SourcePreparer
from app.workflows.modeling import DurableModelingSourceGenerator
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


def _revision_restore_operation_plan(request: McadAgentWorkflowV2Request) -> FreeCADOperationPlan:
    restore = request.revision_restore
    if restore is None:
        raise ValueError("revision restore source is required")
    prefix = f"restore-{restore.source_revision_id.hex}"
    return FreeCADOperationPlan.model_validate({
        "operations": [
            {"op_id": f"{prefix}-inspect", "action": "document.inspect", "args": {}},
            {"op_id": f"{prefix}-export", "action": "document.export", "args": {
                "formats": list(dict.fromkeys(("fcstd", *request.output_formats))),
                "basename": "model",
            }},
        ],
    })


def _worker_id() -> str:
    return f"temporal:{socket.gethostname()}:{os.getpid()}"


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


def _modeling_outputs(step: AgentPlanStep, mode: str) -> tuple[OutputDeclaration, ...]:
    formats = step.output_formats or (("dxf",) if mode == "2d" else ("step",))
    return tuple(
        OutputDeclaration(
            name=output_format,
            media_type=_MODELING_MEDIA_TYPES[output_format],
        )
        for output_format in formats
    )


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


async def _record_freecad_inspections(connection, request, step_key, provenance):
    if provenance.get('engineering_evidence'):
        await append_workflow_event(connection, tenant_id=request.tenant_id, workflow_id=request.workflow_run_id,
            event_type='agent.engineering_evidence.used', payload={'step_key':step_key,
                'references':provenance['engineering_evidence'], 'provider':provenance.get('provider'),
                'model':provenance.get('model'), 'request_hash':provenance.get('request_hash')})
    for index, inspection in enumerate(provenance.get("inspection_calls", [])):
        await append_workflow_event(connection, tenant_id=request.tenant_id,
            workflow_id=request.workflow_run_id, event_type="agent.kernel_inspected",
            payload={"step_key": step_key, "inspection_index": index, **inspection})


class McadWorkflowActivities:
    def __init__(
        self,
        backend: ExecutionBackend | None = None,
        *,
        source_preparer: SourcePreparer | None = None,
        durable_planner: DurableAgentPlanner | None = None,
        durable_modeling: DurableModelingSourceGenerator | None = None,
        durable_repair: DurableRepairSourceGenerator | None = None,
        durable_visual: DurableVisualValidator | None = None,
        freecad_operations: FreeCADOperationGenerator | None = None,
    ):
        self.backend = backend or get_execution_backend()
        self.source_preparer = source_preparer or SourcePreparer(
            Orchestrator(execution_backend=self.backend)
        )
        self.durable_planner = durable_planner or DurableAgentPlanner()
        self.durable_modeling = durable_modeling or DurableModelingSourceGenerator()
        self.durable_repair = durable_repair or DurableRepairSourceGenerator()
        self.durable_visual = durable_visual or DurableVisualValidator()
        self.freecad_operations = freecad_operations or FreeCADOperationGenerator()

    async def _freecad_revision_artifact(
        self,
        request: McadAgentWorkflowV2Request,
        artifact_kind: str,
    ) -> dict[str, Any]:
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            artifact = await committed_artifact_for_revision(
                connection,
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                revision_id=(
                    request.revision_restore.source_revision_id
                    if request.revision_restore else request.expected_base_revision_id
                ),
                artifact_kind=artifact_kind,
            )
        if artifact is None:
            raise ApplicationError(
                f"base revision has no {artifact_kind} artifact",
                type=f"agent_freecad_base_{artifact_kind}_missing",
                non_retryable=True,
            )
        if request.revision_restore is not None and artifact_kind == "fcstd" and (
            artifact["id"] != request.revision_restore.source_artifact_id
            or str(artifact["sha256"]) != request.revision_restore.source_sha256
        ):
            raise ApplicationError(
                "historical FCStd artifact identity differs from the accepted restore request",
                type="revision_restore_source_changed", non_retryable=True,
            )
        return artifact

    async def _freecad_revision_state(
        self,
        request: McadAgentWorkflowV2Request,
    ) -> dict[str, Any]:
        artifact = await self._freecad_revision_artifact(request, "state")
        payload = await get_object(str(artifact["object_key"]))
        if (
            len(payload) != int(artifact["size_bytes"])
            or hashlib.sha256(payload).hexdigest() != str(artifact["sha256"])
        ):
            raise ApplicationError(
                "base revision state artifact failed integrity verification",
                type="agent_freecad_base_state_rejected",
                non_retryable=True,
            )
        try:
            state = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApplicationError(
                "base revision state artifact is not valid JSON",
                type="agent_freecad_base_state_invalid",
                non_retryable=True,
            ) from exc
        if not isinstance(state, dict) or state.get("schema_version") not in {
            "freecad-state.v1",
            "freecad-state.v2",
        }:
            raise ApplicationError(
                "base revision state artifact has an unsupported schema",
                type="agent_freecad_base_state_invalid",
                non_retryable=True,
            )
        # Freeze user-authored meaning at submission time; retries must not read
        # a later collaborator's annotation into an already accepted operation.
        async with tenant_transaction(request.tenant_id, request.principal_id) as conn:
            from app.services.revision_validation import revision_dfm_summary
            state = {**state, "revision_id": str(request.expected_base_revision_id),
                     "dfm_summary": await revision_dfm_summary(conn, request.branch_id, request.expected_base_revision_id)}
            annotations = await conn.scalar(text("SELECT arguments->'_feature_annotations' FROM cad_operations WHERE id=:id"),
                                            {"id": request.workflow_run_id})
            references = await conn.scalar(text("SELECT arguments->'_engineering_evidence' FROM cad_operations WHERE id=:id"),
                                           {"id": request.workflow_run_id})
            if references:
                from app.services.engineering_evidence import verified_engineering_context
                state = {**state, 'engineering_evidence': await verified_engineering_context(conn, references,
                    request.branch_id, request.expected_base_revision_id)}
        if annotations:
            state = {**state, "feature_annotations": annotations}
        return state

    @staticmethod
    def _agent_planning_error(exc: Exception) -> ApplicationError:
        if isinstance(exc, ApplicationError):
            return exc
        if isinstance(exc, ParameterStateError):
            return ApplicationError(
                str(exc),
                type=exc.code,
                non_retryable=True,
            )
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
            base_state: dict[str, Any] | None = None
            if request.revision_restore is not None:
                requirements = ModificationPlan(
                    description=f"从历史 FCStd 恢复版本 {request.revision_restore.source_revision_id}",
                    modification_type="revision_restore",
                )
            elif request.operation == "generate":
                requirements = await self.durable_planner.requirements_generation(
                    request.objective
                )
            else:
                if request.modeling_backend == "freecad":
                    if request.structured_modification is not None:
                        state_artifact = await self._freecad_revision_artifact(
                            request,
                            "state",
                        )
                        if str(state_artifact["sha256"]) != (
                            request.structured_modification.expected_state_sha256
                        ):
                            raise ParameterStateError(
                                "parameter_state_stale",
                                "parameter state hash differs from the committed revision",
                            )
                    base_state = await self._freecad_revision_state(request)
                    if request.structured_modification is not None:
                        if base_state.get("schema_version") != "freecad-state.v2":
                            raise ParameterStateError(
                                "parameter_state_missing",
                                "structured parameter editing requires freecad-state.v2",
                            )
                        compile_parameter_operation_plan(
                            base_state,
                            request.structured_modification.model_dump(mode="json"),
                            output_formats=request.output_formats,
                        )
                        requirements = ModificationPlan(
                            description=request.objective if request.structured_modification.native_edits else "更新已验证的 FreeCAD 参数",
                            modification_type="structured_native_edit" if request.structured_modification.native_edits else "structured_parameter_edit",
                            target_params={
                                update.parameter_id: update.value
                                for update in request.structured_modification.parameter_updates
                            },
                        )
                        result = {
                            "step_key": step_key,
                            "operation": request.operation,
                            "requirements": requirements.model_dump(mode="json"),
                            "base_state": base_state,
                        }
                        async with tenant_transaction(
                            tenant_id,
                            principal_id,
                        ) as connection:
                            return await _complete_agent_logical_step(
                                connection,
                                tenant_id=tenant_id,
                                workflow_id=workflow_id,
                                step_id=step_id,
                                event_type=event_type,
                                result=result,
                            )
                    from app.freecad.semantic_state import bounded_agent_context
                    model_context = json.dumps(
                        bounded_agent_context(base_state),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                else:
                    model_context = request.existing_code or ""
                requirements = await self.durable_planner.requirements_modification(
                    model_context,
                    request.objective,
                )
            result = {
                "step_key": step_key,
                "operation": request.operation,
                "requirements": requirements.model_dump(mode="json"),
                "base_state": base_state,
            }
            if base_state and base_state.get('engineering_evidence'):
                from app.llm import get_last_chat_completion_provenance
                provider = get_last_chat_completion_provenance()
                if provider is None:
                    raise ValueError('工程分析参考的 Agent 规划缺少真实模型调用记录')
                result['engineering_context'] = {**provider,
                    'references':[e['source'] for e in base_state['engineering_evidence']],
                    'context_sha256':hashlib.sha256(model_context.encode('utf-8')).hexdigest()}
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
            if (
                (request.modeling_backend == "freecad" or bool(payload.get("backend_policy_v1")))
                and plan.part_type not in {"assembly", "profile_2d"}
            ):
                decomposition = None
            else:
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
                if (
                    (request.modeling_backend == "freecad" or bool(payload.get("backend_policy_v1")))
                    and requirements.part_type not in {"assembly", "profile_2d"}
                ):
                    plan = self.durable_planner.compose_freecad_generation(
                        request.objective,
                        requirements,
                        output_formats=request.output_formats,
                    )
                else:
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
            if bool(payload.get("backend_policy_v1")):
                plan = normalize_agent_plan_backend(
                    operation=request.operation,
                    request_modeling_backend=request.modeling_backend,
                    plan_candidate=plan,
                )
            if payload.get("validation_repair_v2"):
                plan = enforce_design_validation(plan, autonomous=(
                    request.structured_modification is None and request.revision_restore is None))
            if request.revision_restore is not None:
                restored_plan = plan.temporal_payload()
                # Restoration must fail on invalid history, never redesign it.
                for gate in restored_plan["validation_policy"].values():
                    gate["repair_budget"] = 0
                restored_plan["design_brief"]["acceptance_criteria"] = [
                    "历史 FCStd 可打开并重算，保留原始特征和参数",
                    "新导出产物通过必需几何检查，审查提交前不改变当前版本",
                ]
                restored_plan["confirmation_reason"] = "确认从历史原生文件生成恢复候选；当前版本仅在审查提交后改变。"
                plan = AgentPlan.model_validate(restored_plan)
            plan_payload = plan.temporal_payload()
            if plan.modeling_backend == "freecad" or (
                not bool(payload.get("backend_policy_v1"))
                and request.modeling_backend == "freecad"
                and plan.model_kind not in {"assembly", "profile_2d"}
            ):
                plan_payload["modeling_strategy"] = "freecad_operations"
            result = {
                "step_key": step_key,
                "plan": plan_payload,
                "requires_confirmation": (
                    plan.confirmation_policy.value == "required"
                ),
                "confirmation_reason": plan.confirmation_reason,
                "plan_hash": canonical_sha256(plan_payload),
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

    @activity.defn(name="agent_v2.generate_source")
    async def agent_generate_source(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request = _agent_v2_request(payload)
        plan = AgentPlan.model_validate(payload["plan"])
        step = AgentPlanStep.model_validate(payload["step"])
        step_index = int(payload["step_index"])
        candidate_build_id = _uuid(payload, "candidate_build_id")
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            replay = await get_generated_source_for_step(
                connection,
                tenant_id=request.tenant_id,
                workflow_id=request.workflow_run_id,
                step_key=step.step_key,
            )
            if replay is not None:
                return {
                    "source_id": str(replay["id"]),
                    "source_hash": replay["source_hash"],
                    "source_code": replay["source_code"],
                    "mode": (
                        "2d"
                        if step.output_formats
                        and set(step.output_formats).issubset({"dxf", "svg"})
                        else "3d"
                    ),
                    "generator_kind": replay["generator_kind"],
                    "provenance": {
                        "provider": replay["provider"],
                        "model": replay["model"],
                        "provider_response_id": replay["provider_response_id"],
                        "request_hash": replay["request_hash"],
                        "response_hash": replay["response_hash"],
                        "finish_reason": replay["finish_reason"],
                        "usage": dict(replay["usage"]),
                    },
                    "replayed": True,
                }
            step_id = await _start_agent_logical_step(
                connection,
                tenant_id=request.tenant_id,
                workflow_id=request.workflow_run_id,
                step_key=step.step_key,
                step_index=step_index,
                kind="agent_model",
            )
        try:
            generated = await self.durable_modeling.generate_step_source(
                plan=plan,
                step=step,
                requirements=dict(payload["requirements"]),
                previous_source=(
                    str(payload["previous_source"])
                    if payload.get("previous_source") is not None
                    else None
                ),
                step_index=int(payload["plan_step_index"]),
            )
            provenance = generated.provenance
            async with tenant_transaction(
                request.tenant_id,
                request.principal_id,
            ) as connection:
                recorded = await record_generated_source(
                    connection,
                    tenant_id=request.tenant_id,
                    candidate_build_id=candidate_build_id,
                    workflow_id=request.workflow_run_id,
                    step_id=step_id,
                    predecessor_source_id=(
                        _uuid(payload, "predecessor_source_id")
                        if payload.get("predecessor_source_id")
                        else None
                    ),
                    input_source_ids=tuple(
                        UUID(str(item))
                        for item in payload.get("input_source_ids") or ()
                    ),
                    source_code=generated.source_code,
                    generator_kind=generated.generator_kind,
                    provider=str(provenance["provider"]),
                    model=str(provenance["model"]),
                    provider_response_id=(
                        str(provenance["provider_response_id"])
                        if provenance.get("provider_response_id")
                        else None
                    ),
                    request_hash=str(provenance["request_hash"]),
                    response_hash=str(provenance["response_hash"]),
                    finish_reason=(
                        str(provenance["finish_reason"])
                        if provenance.get("finish_reason")
                        else None
                    ),
                    usage=dict(provenance.get("usage") or {}),
                )
            return {
                "source_id": str(recorded.source_id),
                "source_hash": recorded.source_hash,
                "source_code": generated.source_code,
                "mode": generated.mode,
                "generator_kind": generated.generator_kind,
                "provenance": provenance,
                "replayed": recorded.replayed,
            }
        except Exception as exc:
            error = self._agent_planning_error(exc)
            await _fail_agent_logical_step(
                tenant_id=request.tenant_id,
                principal_id=request.principal_id,
                workflow_id=request.workflow_run_id,
                step_key=step.step_key,
                error_code=error.type or "agent_source_generation_failed",
                error_message=str(error),
            )
            raise error from exc

    @activity.defn(name="agent_v2.repair_source")
    async def agent_repair_source(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request = _agent_v2_request(payload)
        candidate_build_id = _uuid(payload, "candidate_build_id")
        source_id = _uuid(payload, "source_id")
        failure = dict(payload["failure"])
        repair_index = int(payload["repair_index"])
        original_step_key = str(payload["original_step_key"])
        repair_step_key = str(
            payload.get("repair_step_key")
            or f"repair-{original_step_key}-{repair_index:02d}"
        )
        decision = decide_repair(
            category=str(failure["category"]),
            error_code=str(failure["error_code"]),
            error_message=str(failure["error_message"]),
            runtime_error_type=(
                str(failure["runtime_error_type"])
                if failure.get("runtime_error_type")
                else None
            ),
            repair_count=int(payload.get("gate_repair_index", repair_index)) - 1,
            seen_signatures=tuple(
                str(item) for item in payload.get("seen_signatures") or ()
            ),
        )
        if not decision.repairable:
            raise ApplicationError(
                decision.reason,
                {
                    "failure_class": decision.failure_class,
                    "strategy": decision.strategy,
                    "signature": decision.signature,
                },
                type="agent_repair_not_allowed",
                non_retryable=True,
            )
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            replay = await get_generated_source_for_step(
                connection,
                tenant_id=request.tenant_id,
                workflow_id=request.workflow_run_id,
                step_key=repair_step_key,
            )
            if replay is not None:
                return {
                    "source_id": str(replay["id"]),
                    "source_hash": replay["source_hash"],
                    "source_code": replay["source_code"],
                    "repair_step_key": repair_step_key,
                    "failure_class": replay["generator_kind"].removeprefix(
                        "repair:"
                    ),
                    "strategy": str(payload.get("strategy") or decision.strategy),
                    "signature": decision.signature,
                    "provenance": {
                        "provider": replay["provider"],
                        "model": replay["model"],
                        "provider_response_id": replay["provider_response_id"],
                        "request_hash": replay["request_hash"],
                        "response_hash": replay["response_hash"],
                        "finish_reason": replay["finish_reason"],
                        "usage": dict(replay["usage"]),
                    },
                    "replayed": True,
                }
            source = (
                await connection.execute(
                    text(
                        """
                        SELECT source_code, source_hash
                        FROM agent_generated_sources
                        WHERE tenant_id=:tenant_id AND id=:source_id
                          AND candidate_build_id=:candidate_build_id
                        """
                    ),
                    {
                        "tenant_id": request.tenant_id,
                        "source_id": source_id,
                        "candidate_build_id": candidate_build_id,
                    },
                )
            ).mappings().one_or_none()
            if source is None:
                raise ApplicationError(
                    "repair input source does not belong to candidate",
                    type="agent_repair_source_missing",
                    non_retryable=True,
                )
            if str(source["source_hash"]) != str(payload["source_hash"]):
                raise ApplicationError(
                    "repair input source hash does not match persisted source",
                    type="agent_repair_source_hash_mismatch",
                    non_retryable=True,
                )
            step_id = await _start_agent_logical_step(
                connection,
                tenant_id=request.tenant_id,
                workflow_id=request.workflow_run_id,
                step_key=repair_step_key,
                step_index=int(payload["step_index"]),
                kind="agent_repair",
            )
        try:
            repaired = await self.durable_repair.repair(
                source_code=str(source["source_code"]),
                failure=failure,
                decision=decision,
            )
            provenance = repaired.provenance
            async with tenant_transaction(
                request.tenant_id,
                request.principal_id,
            ) as connection:
                recorded = await record_generated_source(
                    connection,
                    tenant_id=request.tenant_id,
                    candidate_build_id=candidate_build_id,
                    workflow_id=request.workflow_run_id,
                    step_id=step_id,
                    predecessor_source_id=source_id,
                    source_code=repaired.source_code,
                    generator_kind=f"repair:{repaired.failure_class}",
                    provider=str(provenance["provider"]),
                    model=str(provenance["model"]),
                    provider_response_id=(
                        str(provenance["provider_response_id"])
                        if provenance.get("provider_response_id")
                        else None
                    ),
                    request_hash=str(provenance["request_hash"]),
                    response_hash=str(provenance["response_hash"]),
                    finish_reason=(
                        str(provenance["finish_reason"])
                        if provenance.get("finish_reason")
                        else None
                    ),
                    usage=dict(provenance.get("usage") or {}),
                )
                await append_workflow_event(
                    connection,
                    tenant_id=request.tenant_id,
                    workflow_id=request.workflow_run_id,
                    event_type="agent.repair.source_generated",
                    payload={
                        "repair_step_key": repair_step_key,
                        "prior_source_id": str(source_id),
                        "source_id": str(recorded.source_id),
                        "prior_source_hash": str(payload["source_hash"]),
                        "source_hash": recorded.source_hash,
                        "failure_class": repaired.failure_class,
                        "strategy": repaired.strategy,
                        "signature": decision.signature,
                        "failure_execution_attempt_id": failure.get(
                            "execution_attempt_id"
                        ),
                        "error_code": failure["error_code"],
                    },
                )
            return {
                "source_id": str(recorded.source_id),
                "source_hash": recorded.source_hash,
                "source_code": repaired.source_code,
                "repair_step_key": repair_step_key,
                "failure_class": repaired.failure_class,
                "strategy": repaired.strategy,
                "signature": decision.signature,
                "provenance": provenance,
                "replayed": recorded.replayed,
            }
        except Exception as exc:
            error = self._agent_planning_error(exc)
            await _fail_agent_logical_step(
                tenant_id=request.tenant_id,
                principal_id=request.principal_id,
                workflow_id=request.workflow_run_id,
                step_key=repair_step_key,
                error_code=error.type or "agent_repair_failed",
                error_message=str(error),
            )
            raise error from exc

    @activity.defn(name="agent_v2.generate_operations")
    async def agent_generate_operations(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request = _agent_v2_request(payload)
        plan = AgentPlan.model_validate(payload["plan"])
        if (
            plan.modeling_backend != "freecad"
            and request.modeling_backend != "freecad"
        ):
            raise ApplicationError(
                "FreeCAD operation generation requires the FreeCAD backend",
                type="invalid_freecad_modeling_backend",
                non_retryable=True,
            )
        step = AgentPlanStep.model_validate(payload["step"])
        candidate_build_id = _uuid(payload, "candidate_build_id")
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            replay = await get_generated_source_for_step(
                connection,
                tenant_id=request.tenant_id,
                workflow_id=request.workflow_run_id,
                step_key=step.step_key,
            )
            if replay is not None:
                FreeCADOperationPlan.model_validate_json(replay["source_code"])
                return {
                    "source_id": str(replay["id"]),
                    "source_hash": replay["source_hash"],
                    "source_code": replay["source_code"],
                    "mode": "3d",
                    "generator_kind": replay["generator_kind"],
                    "provenance": {
                        "provider": replay["provider"],
                        "model": replay["model"],
                        "provider_response_id": replay["provider_response_id"],
                        "request_hash": replay["request_hash"],
                        "response_hash": replay["response_hash"],
                        "finish_reason": replay["finish_reason"],
                        "usage": dict(replay["usage"]),
                    },
                    "replayed": True,
                }
            step_id = await _start_agent_logical_step(
                connection,
                tenant_id=request.tenant_id,
                workflow_id=request.workflow_run_id,
                step_key=step.step_key,
                step_index=int(payload["step_index"]),
                kind="agent_freecad_operations",
            )
        try:
            if request.revision_restore is not None:
                operation_plan = _revision_restore_operation_plan(request)
                source_code = operation_plan.model_dump_json()
                generator_kind = "native-revision-restore-v1"
                provenance = {
                    "provider": "cad-agent", "model": generator_kind,
                    "provider_response_id": None,
                    "request_hash": canonical_sha256(request.revision_restore.model_dump(mode="json")),
                    "response_hash": hashlib.sha256(source_code.encode("utf-8")).hexdigest(),
                    "finish_reason": "deterministic", "usage": {},
                }
            elif request.structured_modification is not None:
                operation_plan = compile_parameter_operation_plan(
                    dict(payload["base_state"]),
                    request.structured_modification.model_dump(mode="json"),
                    output_formats=request.output_formats,
                )
                source_code = operation_plan.model_dump_json()
                source_hash = hashlib.sha256(
                    source_code.encode("utf-8")
                ).hexdigest()
                generator_kind = "structured-native-compiler-v1" if request.structured_modification.native_edits else "structured-parameter-compiler-v1"
                provenance = {
                    "provider": "cad-agent",
                    "model": generator_kind,
                    "provider_response_id": None,
                    "request_hash": canonical_sha256({
                        "state_sha256": (
                            request.structured_modification.expected_state_sha256
                        ),
                        "parameter_updates": [
                            update.model_dump(mode="json")
                            for update in request.structured_modification.parameter_updates
                        ],
                        **({'native_edits':[edit.model_dump(mode='json') for edit in request.structured_modification.native_edits]}
                           if request.structured_modification.native_edits else {}),
                    }),
                    "response_hash": source_hash,
                    "finish_reason": "deterministic",
                    "usage": {},
                }
            else:
                generated = await _await_provider_operation(self.freecad_operations.generate(
                    plan=plan,
                    requirements=dict(payload["requirements"]),
                    base_state=(
                        dict(payload["base_state"])
                        if payload.get("base_state") is not None
                        else None
                    ),
                    output_formats=request.output_formats,
                ))
                source_code = generated.source_code
                generator_kind = generated.generator_kind
                provenance = generated.provenance
            async with tenant_transaction(
                request.tenant_id,
                request.principal_id,
            ) as connection:
                recorded = await record_generated_source(
                    connection,
                    tenant_id=request.tenant_id,
                    candidate_build_id=candidate_build_id,
                    workflow_id=request.workflow_run_id,
                    step_id=step_id,
                    source_code=source_code,
                    generator_kind=generator_kind,
                    provider=str(provenance["provider"]),
                    model=str(provenance["model"]),
                    provider_response_id=(
                        str(provenance["provider_response_id"])
                        if provenance.get("provider_response_id")
                        else None
                    ),
                    request_hash=str(provenance["request_hash"]),
                    response_hash=str(provenance["response_hash"]),
                    finish_reason=(
                        str(provenance["finish_reason"])
                        if provenance.get("finish_reason")
                        else None
                    ),
                    usage=dict(provenance.get("usage") or {}),
                )
                if not recorded.replayed:
                    await _record_freecad_inspections(connection, request, step.step_key, provenance)
            return {
                "source_id": str(recorded.source_id),
                "source_hash": recorded.source_hash,
                "source_code": source_code,
                "mode": "3d",
                "generator_kind": generator_kind,
                "provenance": provenance,
                "replayed": recorded.replayed,
            }
        except Exception as exc:
            error = self._agent_planning_error(exc)
            await _fail_agent_logical_step(
                tenant_id=request.tenant_id,
                principal_id=request.principal_id,
                workflow_id=request.workflow_run_id,
                step_key=step.step_key,
                error_code=error.type or "agent_freecad_operation_generation_failed",
                error_message=str(error),
            )
            raise error from exc

    @activity.defn(name="agent_v2.repair_operations")
    async def agent_repair_operations(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        request = _agent_v2_request(payload)
        if request.revision_restore is not None:
            raise ApplicationError(
                "historical revision restoration cannot rewrite model operations",
                type="revision_restore_repair_forbidden", non_retryable=True,
            )
        if request.structured_modification is not None:
            raise ApplicationError(
                "明确提交的原生修改或参数修改未通过检查，请调整输入后重新提交。原始检查："
                + str((payload.get('failure') or {}).get('error_message') or '未提供详细错误')[:2500],
                type="native_edit_repair_forbidden", non_retryable=True,
            )
        candidate_build_id = _uuid(payload, "candidate_build_id")
        source_id = _uuid(payload, "source_id")
        failure = dict(payload["failure"])
        repair_index = int(payload["repair_index"])
        original_step_key = str(payload["original_step_key"])
        repair_step_key = str(
            payload.get("repair_step_key")
            or f"repair-{original_step_key}-{repair_index:02d}"
        )
        decision = decide_repair(
            category=str(failure["category"]),
            error_code=str(failure["error_code"]),
            error_message=str(failure["error_message"]),
            runtime_error_type=(
                str(failure["runtime_error_type"])
                if failure.get("runtime_error_type")
                else None
            ),
            repair_count=int(payload.get("gate_repair_index", repair_index)) - 1,
            seen_signatures=tuple(
                str(item) for item in payload.get("seen_signatures") or ()
            ),
        )
        if not decision.repairable:
            raise ApplicationError(
                decision.reason,
                {
                    "failure_class": decision.failure_class,
                    "strategy": decision.strategy,
                    "signature": decision.signature,
                },
                type="agent_freecad_repair_not_allowed",
                non_retryable=True,
            )
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            replay = await get_generated_source_for_step(
                connection,
                tenant_id=request.tenant_id,
                workflow_id=request.workflow_run_id,
                step_key=repair_step_key,
            )
            if replay is not None:
                FreeCADOperationPlan.model_validate_json(replay["source_code"])
                return {
                    "source_id": str(replay["id"]),
                    "source_hash": replay["source_hash"],
                    "source_code": replay["source_code"],
                    "repair_step_key": repair_step_key,
                    "failure_class": decision.failure_class,
                    "strategy": decision.strategy,
                    "signature": decision.signature,
                    "replayed": True,
                }
            source = (
                await connection.execute(
                    text(
                        """
                        SELECT source_code, source_hash
                        FROM agent_generated_sources
                        WHERE tenant_id=:tenant_id AND id=:source_id
                          AND candidate_build_id=:candidate_build_id
                        """
                    ),
                    {
                        "tenant_id": request.tenant_id,
                        "source_id": source_id,
                        "candidate_build_id": candidate_build_id,
                    },
                )
            ).mappings().one_or_none()
            if source is None:
                raise ApplicationError(
                    "FreeCAD repair input does not belong to candidate",
                    type="agent_freecad_repair_source_missing",
                    non_retryable=True,
                )
            if str(source["source_hash"]) != str(payload["source_hash"]):
                raise ApplicationError(
                    "FreeCAD repair input hash does not match persisted source",
                    type="agent_freecad_repair_source_hash_mismatch",
                    non_retryable=True,
                )
            step_id = await _start_agent_logical_step(
                connection,
                tenant_id=request.tenant_id,
                workflow_id=request.workflow_run_id,
                step_key=repair_step_key,
                step_index=int(payload["step_index"]),
                kind="agent_freecad_repair",
            )
        try:
            repaired = await _await_provider_operation(self.freecad_operations.repair(
                source_code=str(source["source_code"]),
                failure=failure,
                base_state=(
                    dict(payload["base_state"])
                    if payload.get("base_state") is not None
                    else None
                ),
                output_formats=request.output_formats,
            ))
            provenance = repaired.provenance
            async with tenant_transaction(
                request.tenant_id,
                request.principal_id,
            ) as connection:
                recorded = await record_generated_source(
                    connection,
                    tenant_id=request.tenant_id,
                    candidate_build_id=candidate_build_id,
                    workflow_id=request.workflow_run_id,
                    step_id=step_id,
                    predecessor_source_id=source_id,
                    source_code=repaired.source_code,
                    generator_kind=f"repair:{decision.failure_class}:freecad_operations",
                    provider=str(provenance["provider"]),
                    model=str(provenance["model"]),
                    provider_response_id=(
                        str(provenance["provider_response_id"])
                        if provenance.get("provider_response_id")
                        else None
                    ),
                    request_hash=str(provenance["request_hash"]),
                    response_hash=str(provenance["response_hash"]),
                    finish_reason=(
                        str(provenance["finish_reason"])
                        if provenance.get("finish_reason")
                        else None
                    ),
                    usage=dict(provenance.get("usage") or {}),
                )
                if not recorded.replayed:
                    await _record_freecad_inspections(connection, request, repair_step_key, provenance)
                await append_workflow_event(
                    connection,
                    tenant_id=request.tenant_id,
                    workflow_id=request.workflow_run_id,
                    event_type="agent.freecad.operations_repaired",
                    payload={
                        "repair_step_key": repair_step_key,
                        "prior_source_id": str(source_id),
                        "source_id": str(recorded.source_id),
                        "source_hash": recorded.source_hash,
                        "failure_class": decision.failure_class,
                        "strategy": decision.strategy,
                        "signature": decision.signature,
                        "error_code": failure["error_code"],
                    },
                )
            return {
                "source_id": str(recorded.source_id),
                "source_hash": recorded.source_hash,
                "source_code": repaired.source_code,
                "repair_step_key": repair_step_key,
                "failure_class": decision.failure_class,
                "strategy": decision.strategy,
                "signature": decision.signature,
                "provenance": provenance,
                "replayed": recorded.replayed,
            }
        except Exception as exc:
            error = self._agent_planning_error(exc)
            await _fail_agent_logical_step(
                tenant_id=request.tenant_id,
                principal_id=request.principal_id,
                workflow_id=request.workflow_run_id,
                step_key=repair_step_key,
                error_code=error.type or "agent_freecad_repair_failed",
                error_message=str(error),
            )
            raise error from exc

    @activity.defn(name="agent_v2.execute_model")
    async def agent_execute_model(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        info = activity.info()
        request = _agent_v2_request(payload)
        plan = AgentPlan.model_validate(payload["plan"])
        step = AgentPlanStep.model_validate(payload["step"])
        run_step_key = str(payload.get("run_step_key") or step.step_key)
        candidate_build_id = _uuid(payload, "candidate_build_id")
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            replay = await get_staging_manifest_for_step(
                connection,
                tenant_id=request.tenant_id,
                candidate_build_id=candidate_build_id,
                workflow_id=request.workflow_run_id,
                step_key=run_step_key,
            )
            if replay is not None:
                return {
                    **dict(replay["result_payload"] or {}),
                    "staging_manifest_id": str(replay["id"]),
                    "manifest_hash": replay["manifest_hash"],
                    "manifest": dict(replay["manifest"]),
                    "replayed": True,
                }

        attempt_id, step_id, lease_token, lease_generation = (
            await _prepare_agent_execution_attempt(
                payload,
                temporal_attempt=info.attempt,
            )
        )
        outcome: MaterializedExecutionOutcome | None = None
        upload_heartbeat: asyncio.Task | None = None
        try:
            snapshot = await asyncio.to_thread(self.backend.runtime_snapshot)
            source_code = str(payload["source_code"])
            source_hash = hashlib.sha256(source_code.encode("utf-8")).hexdigest()
            if source_hash != str(payload["source_hash"]):
                raise ApplicationError(
                    "modeling source hash does not match persisted source",
                    type="agent_source_hash_mismatch",
                    non_retryable=True,
                )
            outputs = _modeling_outputs(step, str(payload["mode"]))
            spec = ExecutionSpec(
                execution_attempt_id=str(attempt_id),
                workflow_run_id=str(request.workflow_run_id),
                step_run_id=str(step_id),
                tenant_id=str(request.tenant_id),
                project_id=str(request.project_id),
                expected_base_revision_id=str(request.expected_base_revision_id),
                idempotency_key=(
                    f"agent-v2:{request.workflow_run_id}:{run_step_key}:"
                    f"attempt:{info.attempt}"
                ),
                capability="mcad.model",
                operation=plan.operation,
                mode=str(payload["mode"]),
                source=ExecutionSource(
                    language="python",
                    code=source_code,
                    sha256=source_hash,
                ),
                outputs=outputs,
                runtime=RuntimeRequirement(
                    image_digest=snapshot.image_digest,
                    platform=snapshot.platform,
                    sandbox_tier="ephemeral-job",
                ),
                limits=ResourceLimits(timeout_seconds=120),
                metadata={
                    "candidate_build_id": str(candidate_build_id),
                    "source_id": str(payload["source_id"]),
                    "plan_step_key": step.step_key,
                    "run_step_key": run_step_key,
                    "temporal_activity_id": info.activity_id,
                    "temporal_attempt": info.attempt,
                },
            )
            outcome = await _run_backend_with_heartbeats(
                self.backend,
                spec,
                tenant_id=request.tenant_id,
                principal_id=request.principal_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                lease_generation=lease_generation,
            )
            if activity.is_cancelled():
                raise asyncio.CancelledError
            if outcome.result.status is not ExecutionStatus.SUCCEEDED:
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
                    error=error,
                )
                retryable = bool(error and error.retryable)
                raise ApplicationError(
                    message,
                    {
                        "execution_attempt_id": str(attempt_id),
                        "category": error.category.value if error else "internal",
                        "error_code": code,
                        "error_message": message,
                        "runtime_error_type": (
                            error.evidence.get("runtime_error_type")
                            if error
                            else None
                        ),
                    },
                    type=code,
                    non_retryable=not retryable,
                )

            upload_heartbeat = asyncio.create_task(
                _heartbeat_loop(
                    tenant_id=request.tenant_id,
                    principal_id=request.principal_id,
                    attempt_id=attempt_id,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                )
            )
            staged_outputs = []
            for output_name, path in outcome.files.items():
                declared = next(
                    item
                    for item in outcome.result.outputs
                    if item.name == path.name
                )
                object_key = (
                    "staging/agent/tenants/"
                    f"{request.tenant_id}/candidates/{candidate_build_id}/"
                    f"attempts/{attempt_id}/{declared.sha256}/"
                    f"{Path(declared.name).name}"
                )
                uploaded = await put_file(
                    object_key,
                    path,
                    content_type=declared.media_type,
                )
                if (
                    uploaded["sha256"] != declared.sha256
                    or uploaded["size_bytes"] != declared.size_bytes
                ):
                    raise ApplicationError(
                        "staged object does not match executor declaration",
                        type="agent_staging_integrity_failed",
                        non_retryable=True,
                    )
                staged_outputs.append(
                    {
                        "format": output_name,
                        "filename": Path(declared.name).name,
                        "object_key": object_key,
                        "sha256": declared.sha256,
                        "size_bytes": declared.size_bytes,
                        "content_type": declared.media_type,
                    }
                )
            upload_heartbeat.cancel()
            try:
                await upload_heartbeat
            except asyncio.CancelledError:
                pass
            upload_heartbeat = None
            manifest = {
                "schema_version": "agent-staging-manifest.v1",
                "candidate_build_id": str(candidate_build_id),
                "workflow_run_id": str(request.workflow_run_id),
                "step_run_id": str(step_id),
                "execution_attempt_id": str(attempt_id),
                "source_id": str(payload["source_id"]),
                "source_hash": source_hash,
                "plan_step_key": step.step_key,
                "run_step_key": run_step_key,
                "outputs": staged_outputs,
                "runtime_provenance": (
                    outcome.result.provenance.model_dump(mode="json")
                    if outcome.result.provenance
                    else None
                ),
            }
            result_payload = {
                "status": "succeeded",
                "attempt_id": str(attempt_id),
                "source_id": str(payload["source_id"]),
                "source_hash": source_hash,
                "outputs": staged_outputs,
                "execution_result": outcome.result.model_dump(mode="json"),
            }
            async with tenant_transaction(
                request.tenant_id,
                request.principal_id,
            ) as connection:
                await complete_attempt(
                    connection,
                    attempt_id,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                    result_payload=result_payload,
                )
                accepted = await accept_staging_manifest(
                    connection,
                    tenant_id=request.tenant_id,
                    candidate_build_id=candidate_build_id,
                    workflow_id=request.workflow_run_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    lease_generation=lease_generation,
                    manifest=manifest,
                    lease_token=lease_token,
                    supersedes_id=(
                        _uuid(payload, "supersedes_staging_manifest_id")
                        if payload.get("supersedes_staging_manifest_id")
                        else None
                    ),
                )
                await transition_step(
                    connection,
                    step_id,
                    expected=StepStatus.RUNNING,
                    target=StepStatus.SUCCEEDED,
                )
            return {
                **result_payload,
                "staging_manifest_id": str(accepted.staging_manifest_id),
                "manifest_hash": accepted.manifest_hash,
                "manifest": manifest,
                "replayed": accepted.replayed,
            }
        except asyncio.CancelledError:
            await _mark_execution_failure(
                payload,
                attempt_id=attempt_id,
                step_id=step_id,
                status=ExecutionStatus.CANCELLED,
                error_code="execution_cancelled",
                error_message="Durable Agent modeling execution was cancelled.",
            )
            raise
        except ApplicationError as exc:
            await _mark_execution_failure(
                payload,
                attempt_id=attempt_id,
                step_id=step_id,
                status=ExecutionStatus.FAILED,
                error_code=(exc.type or "agent_model_execution_failed")[:200],
                error_message=str(exc)[:4000],
            )
            raise
        except Exception as exc:
            await _mark_execution_failure(
                payload,
                attempt_id=attempt_id,
                step_id=step_id,
                status=ExecutionStatus.FAILED,
                error_code="agent_model_execution_failed",
                error_message=str(exc)[:4000],
            )
            raise ApplicationError(
                str(exc)[:4000],
                type="agent_model_execution_failed",
            ) from exc
        finally:
            if upload_heartbeat is not None:
                upload_heartbeat.cancel()
                try:
                    await upload_heartbeat
                except asyncio.CancelledError:
                    pass
            if outcome is not None and outcome.work_dir is not None:
                shutil.rmtree(outcome.work_dir, ignore_errors=True)

    @activity.defn(name="agent_v2.execute_freecad")
    async def agent_execute_freecad(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        info = activity.info()
        request = _agent_v2_request(payload)
        plan = AgentPlan.model_validate(payload["plan"])
        if (
            plan.modeling_backend != "freecad"
            and request.modeling_backend != "freecad"
        ):
            raise ApplicationError(
                "FreeCAD execution requires the FreeCAD backend",
                type="invalid_freecad_modeling_backend",
                non_retryable=True,
            )
        step = AgentPlanStep.model_validate(payload["step"])
        run_step_key = str(payload.get("run_step_key") or step.step_key)
        candidate_build_id = _uuid(payload, "candidate_build_id")
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            replay = await get_staging_manifest_for_step(
                connection,
                tenant_id=request.tenant_id,
                candidate_build_id=candidate_build_id,
                workflow_id=request.workflow_run_id,
                step_key=run_step_key,
            )
            if replay is not None:
                return {
                    **dict(replay["result_payload"] or {}),
                    "staging_manifest_id": str(replay["id"]),
                    "manifest_hash": replay["manifest_hash"],
                    "manifest": dict(replay["manifest"]),
                    "replayed": True,
                }
            head_revision_id = await connection.scalar(
                text(
                    """
                    SELECT head_revision_id FROM project_branches
                    WHERE tenant_id=:tenant_id AND project_id=:project_id
                      AND id=:branch_id
                    """
                ),
                {
                    "tenant_id": request.tenant_id,
                    "project_id": request.project_id,
                    "branch_id": request.branch_id,
                },
            )
        if head_revision_id is None:
            raise ApplicationError(
                "FreeCAD execution branch does not exist",
                type="agent_freecad_branch_missing",
                non_retryable=True,
            )
        if head_revision_id != request.expected_base_revision_id:
            raise ApplicationError(
                "expected base revision is no longer the branch head",
                type="stale_base_revision",
                non_retryable=True,
            )

        source_code = str(payload["source_code"])
        source_hash = hashlib.sha256(source_code.encode("utf-8")).hexdigest()
        if source_hash != str(payload["source_hash"]):
            raise ApplicationError(
                "FreeCAD operation plan hash does not match persisted source",
                type="agent_source_hash_mismatch",
                non_retryable=True,
            )
        try:
            operation_plan = FreeCADOperationPlan.model_validate_json(source_code)
        except Exception as exc:
            raise ApplicationError(
                "persisted FreeCAD operation plan is invalid",
                type="agent_freecad_operation_plan_invalid",
                non_retryable=True,
            ) from exc

        temp_dir = Path(tempfile.mkdtemp(prefix="agent_freecad_"))
        declarations: list[ArtifactInput] = []
        materialized: dict[str, Path] = {}
        task_inputs: dict[str, str] = {}
        try:
            if request.operation == "modify":
                artifact = await self._freecad_revision_artifact(request, "fcstd")
                base_path = temp_dir / "base.FCStd"
                downloaded = await download_object(
                    str(artifact["object_key"]),
                    base_path,
                )
                if (
                    downloaded["sha256"] != str(artifact["sha256"])
                    or downloaded["size_bytes"] != int(artifact["size_bytes"])
                ):
                    raise ApplicationError(
                        "base FCStd artifact failed integrity verification",
                        type="agent_freecad_base_fcstd_rejected",
                        non_retryable=True,
                    )
                input_revision_id = (
                    request.revision_restore.source_revision_id
                    if request.revision_restore else request.expected_base_revision_id
                )
                artifact_id = f"{input_revision_id}:fcstd"
                declarations.append(
                    ArtifactInput(
                        artifact_id=artifact_id,
                        filename=base_path.name,
                        sha256=str(artifact["sha256"]),
                        size_bytes=int(artifact["size_bytes"]),
                        media_type=str(artifact["content_type"]),
                    )
                )
                materialized[artifact_id] = base_path
                task_inputs["base"] = base_path.name

            task = {
                "schema_version": "mcad-capability-task.v1",
                "capability": "freecad",
                "operation": "execute",
                "params": {
                    "plan": operation_plan.model_dump(mode="json"),
                    "expected_revision_id": str(request.expected_base_revision_id),
                },
                "inputs": task_inputs,
            }
            if request.revision_restore is not None:
                task["params"]["expected_revision_id"] = str(request.revision_restore.source_revision_id)
            task_source = json.dumps(
                task,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            export_formats = tuple(
                operation_plan.operations[-1].typed_args().formats
            )
            outputs = tuple(
                [
                    OutputDeclaration(
                        name=name,
                        media_type=_MODELING_MEDIA_TYPES[name],
                        max_size_bytes=128 * 1024 * 1024,
                    )
                    for name in (*export_formats, "state")
                ]
                + [
                    OutputDeclaration(
                        name="capability-result",
                        media_type="application/json",
                        max_size_bytes=2 * 1024 * 1024,
                    )
                ]
            )
            attempt_id, step_id, lease_token, lease_generation = (
                await _prepare_agent_execution_attempt(
                    payload,
                    temporal_attempt=info.attempt,
                )
            )
            outcome: MaterializedExecutionOutcome | None = None
            upload_heartbeat: asyncio.Task | None = None
            try:
                snapshot = await asyncio.to_thread(self.backend.runtime_snapshot)
                spec = ExecutionSpec(
                    execution_attempt_id=str(attempt_id),
                    workflow_run_id=str(request.workflow_run_id),
                    step_run_id=str(step_id),
                    tenant_id=str(request.tenant_id),
                    project_id=str(request.project_id),
                    expected_base_revision_id=str(
                        request.expected_base_revision_id
                    ),
                    idempotency_key=(
                        f"agent-v2:freecad:{request.workflow_run_id}:"
                        f"{run_step_key}:attempt:{info.attempt}"
                    ),
                    capability="mcad.freecad",
                    operation="execute",
                    mode="3d",
                    source=ExecutionSource(
                        language="json",
                        code=task_source,
                        sha256=hashlib.sha256(
                            task_source.encode("utf-8")
                        ).hexdigest(),
                    ),
                    inputs=tuple(declarations),
                    outputs=outputs,
                    runtime=RuntimeRequirement(
                        image_digest=snapshot.image_digest,
                        platform=snapshot.platform,
                        sandbox_tier="ephemeral-job",
                    ),
                    limits=ResourceLimits(
                        timeout_seconds=int(payload.get("timeout_seconds") or 180),
                        memory_bytes=1536 * 1024 * 1024,
                        cpu_millis=2000,
                        pids=512,
                        output_bytes=384 * 1024 * 1024,
                    ),
                    metadata={
                        "candidate_build_id": str(candidate_build_id),
                        "source_id": str(payload["source_id"]),
                        "plan_step_key": step.step_key,
                        "run_step_key": run_step_key,
                        "modeling_backend": "freecad",
                        "temporal_activity_id": info.activity_id,
                        "temporal_attempt": info.attempt,
                    },
                )
                outcome = await _run_backend_with_heartbeats(
                    self.backend,
                    spec,
                    tenant_id=request.tenant_id,
                    principal_id=request.principal_id,
                    attempt_id=attempt_id,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                    materialized_inputs=materialized,
                )
                if activity.is_cancelled():
                    raise asyncio.CancelledError
                if outcome.result.status is not ExecutionStatus.SUCCEEDED:
                    error = outcome.result.error
                    code = error.code if error else "freecad_execution_failed"
                    message = error.message if error else "FreeCAD execution failed"
                    await _mark_execution_failure(
                        payload,
                        attempt_id=attempt_id,
                        step_id=step_id,
                        status=outcome.result.status,
                        error_code=code,
                        error_message=message,
                        error=error,
                    )
                    retryable = bool(error and error.retryable)
                    raise ApplicationError(
                        message,
                        {
                            "execution_attempt_id": str(attempt_id),
                            "category": (
                                error.category.value if error else "cad_kernel"
                            ),
                            "error_code": code,
                            "error_message": message,
                            "runtime_error_type": (
                                error.evidence.get("runtime_error_type")
                                if error
                                else None
                            ),
                        },
                        type=code,
                        non_retryable=not retryable,
                    )

                upload_heartbeat = asyncio.create_task(
                    _heartbeat_loop(
                        tenant_id=request.tenant_id,
                        principal_id=request.principal_id,
                        attempt_id=attempt_id,
                        lease_token=lease_token,
                        lease_generation=lease_generation,
                    )
                )
                staged_outputs: list[dict[str, Any]] = []
                for output_name, path in outcome.files.items():
                    declared = next(
                        item
                        for item in outcome.result.outputs
                        if item.name == path.name
                    )
                    object_key = (
                        "staging/agent/tenants/"
                        f"{request.tenant_id}/candidates/{candidate_build_id}/"
                        f"attempts/{attempt_id}/{declared.sha256}/"
                        f"{Path(declared.name).name}"
                    )
                    uploaded = await put_file(
                        object_key,
                        path,
                        content_type=declared.media_type,
                    )
                    if (
                        uploaded["sha256"] != declared.sha256
                        or uploaded["size_bytes"] != declared.size_bytes
                    ):
                        raise ApplicationError(
                            "staged FreeCAD object does not match declaration",
                            type="agent_staging_integrity_failed",
                            non_retryable=True,
                        )
                    staged_outputs.append(
                        {
                            "format": output_name,
                            "filename": Path(declared.name).name,
                            "object_key": object_key,
                            "sha256": declared.sha256,
                            "size_bytes": declared.size_bytes,
                            "content_type": declared.media_type,
                        }
                    )
                upload_heartbeat.cancel()
                try:
                    await upload_heartbeat
                except asyncio.CancelledError:
                    pass
                upload_heartbeat = None
                manifest = {
                    "schema_version": "agent-staging-manifest.v1",
                    "candidate_build_id": str(candidate_build_id),
                    "workflow_run_id": str(request.workflow_run_id),
                    "step_run_id": str(step_id),
                    "execution_attempt_id": str(attempt_id),
                    "source_id": str(payload["source_id"]),
                    "source_hash": source_hash,
                    "plan_step_key": step.step_key,
                    "run_step_key": run_step_key,
                    "modeling_backend": "freecad",
                    "base_revision_id": str(request.expected_base_revision_id),
                    "outputs": staged_outputs,
                    "runtime_provenance": (
                        outcome.result.provenance.model_dump(mode="json")
                        if outcome.result.provenance
                        else None
                    ),
                }
                if request.revision_restore is not None:
                    manifest["revision_restore"] = request.revision_restore.model_dump(mode="json")
                result_payload = {
                    "status": "succeeded",
                    "attempt_id": str(attempt_id),
                    "source_id": str(payload["source_id"]),
                    "source_hash": source_hash,
                    "outputs": staged_outputs,
                    "execution_result": outcome.result.model_dump(mode="json"),
                }
                async with tenant_transaction(
                    request.tenant_id,
                    request.principal_id,
                ) as connection:
                    await complete_attempt(
                        connection,
                        attempt_id,
                        lease_token=lease_token,
                        lease_generation=lease_generation,
                        result_payload=result_payload,
                    )
                    accepted = await accept_staging_manifest(
                        connection,
                        tenant_id=request.tenant_id,
                        candidate_build_id=candidate_build_id,
                        workflow_id=request.workflow_run_id,
                        step_id=step_id,
                        attempt_id=attempt_id,
                        lease_generation=lease_generation,
                        manifest=manifest,
                        lease_token=lease_token,
                        supersedes_id=(
                            _uuid(payload, "supersedes_staging_manifest_id")
                            if payload.get("supersedes_staging_manifest_id")
                            else None
                        ),
                    )
                    await transition_step(
                        connection,
                        step_id,
                        expected=StepStatus.RUNNING,
                        target=StepStatus.SUCCEEDED,
                    )
                return {
                    **result_payload,
                    "staging_manifest_id": str(accepted.staging_manifest_id),
                    "manifest_hash": accepted.manifest_hash,
                    "manifest": manifest,
                    "replayed": accepted.replayed,
                }
            except asyncio.CancelledError:
                await _mark_execution_failure(
                    payload,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    status=ExecutionStatus.CANCELLED,
                    error_code="freecad_execution_cancelled",
                    error_message="FreeCAD execution was cancelled.",
                )
                raise
            except ApplicationError as exc:
                await _mark_execution_failure(
                    payload,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    status=ExecutionStatus.FAILED,
                    error_code=(exc.type or "agent_freecad_execution_failed")[:200],
                    error_message=str(exc)[:4000],
                )
                raise
            except Exception as exc:
                await _mark_execution_failure(
                    payload,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    status=ExecutionStatus.FAILED,
                    error_code="agent_freecad_execution_failed",
                    error_message=str(exc)[:4000],
                )
                raise ApplicationError(
                    str(exc)[:4000],
                    type="agent_freecad_execution_failed",
                ) from exc
            finally:
                if upload_heartbeat is not None:
                    upload_heartbeat.cancel()
                    try:
                        await upload_heartbeat
                    except asyncio.CancelledError:
                        pass
                if outcome is not None and outcome.work_dir is not None:
                    shutil.rmtree(outcome.work_dir, ignore_errors=True)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @activity.defn(name="agent_v2.validate_geometry")
    async def agent_validate_geometry(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        info = activity.info()
        request = _agent_v2_request(payload)
        candidate_build_id = _uuid(payload, "candidate_build_id")
        manifest_id = _uuid(payload, "staging_manifest_id")
        step_key = str(payload["validation_step_key"])
        step_index = int(payload["step_index"])
        expected_dimensions = {
            str(key): float(value)
            for key, value in dict(
                payload.get("expected_dimensions_mm") or {}
            ).items()
        }
        dimension_tolerance = float(payload.get("dimension_tolerance", 0.05))
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            replay = await get_validation_evidence_for_manifest(
                connection,
                tenant_id=request.tenant_id,
                candidate_build_id=candidate_build_id,
                workflow_id=request.workflow_run_id,
                staging_manifest_id=manifest_id,
                gate="geometry",
            )
            if replay is not None:
                return {
                    "status": "completed",
                    "gate": "geometry",
                    "mode": replay["mode"],
                    "outcome": replay["outcome"],
                    "evidence_id": str(replay["id"]),
                    "evidence_hash": replay["evidence_hash"],
                    "report": dict(replay["evidence"]),
                    "attempt_id": (
                        str(replay["execution_attempt_id"])
                        if replay["execution_attempt_id"]
                        else None
                    ),
                    "replayed": True,
                }
            manifest_row = (
                await connection.execute(
                    text(
                        """
                        SELECT manifest FROM agent_staging_manifests
                        WHERE tenant_id=:tenant_id
                          AND candidate_build_id=:candidate_build_id
                          AND workflow_run_id=:workflow_id AND id=:manifest_id
                        """
                    ),
                    {
                        "tenant_id": request.tenant_id,
                        "candidate_build_id": candidate_build_id,
                        "workflow_id": request.workflow_run_id,
                        "manifest_id": manifest_id,
                    },
                )
            ).mappings().one_or_none()
        if manifest_row is None:
            raise ApplicationError(
                "geometry validation manifest does not exist",
                type="agent_geometry_manifest_missing",
                non_retryable=True,
            )
        manifest = dict(manifest_row["manifest"])
        model_outputs = tuple(
            dict(item)
            for item in manifest.get("outputs") or ()
            if str(item.get("format") or "").lower() in {"step", "stl", "dxf"}
        )
        if not model_outputs:
            raise ApplicationError(
                "geometry validation requires STEP, STL, or DXF output",
                type="agent_geometry_input_missing",
                non_retryable=True,
            )
        attempt_id, step_id, lease_token, lease_generation = (
            await _prepare_agent_validation_attempt(
                payload,
                temporal_attempt=info.attempt,
                step_key=step_key,
                step_index=step_index,
                step_kind="agent_geometry_validation",
            )
        )
        temp_dir = Path(tempfile.mkdtemp(prefix="agent_geometry_"))
        outcome: MaterializedExecutionOutcome | None = None
        execution_status: ExecutionStatus
        result_payload: dict[str, Any] | None = None
        terminal_error_code: str | None = None
        terminal_error_message: str | None = None
        try:
            declarations: list[ArtifactInput] = []
            materialized: dict[str, Path] = {}
            task_inputs: dict[str, str] = {}
            task_artifacts: list[dict[str, str]] = []
            integrity_issue: str | None = None
            for index, item in enumerate(model_outputs):
                artifact_format = str(item["format"]).lower()
                role = f"artifact-{index:02d}"
                filename = f"geometry-{index:02d}.{artifact_format}"
                path = temp_dir / filename
                try:
                    downloaded = await download_object(
                        str(item["object_key"]),
                        path,
                    )
                except Exception as exc:
                    integrity_issue = f"object_unavailable:{type(exc).__name__}"
                    break
                if (
                    downloaded["sha256"] != str(item["sha256"])
                    or downloaded["size_bytes"] != int(item["size_bytes"])
                ):
                    integrity_issue = "object_integrity_mismatch"
                    break
                artifact_id = f"{manifest_id}:{index}"
                declarations.append(
                    ArtifactInput(
                        artifact_id=artifact_id,
                        filename=filename,
                        sha256=str(item["sha256"]),
                        size_bytes=int(item["size_bytes"]),
                        media_type=str(item["content_type"]),
                    )
                )
                materialized[artifact_id] = path
                task_inputs[role] = filename
                task_artifacts.append(
                    {"role": role, "format": artifact_format}
                )
            if integrity_issue is not None:
                report = indeterminate_geometry_report(
                    outputs=model_outputs,
                    expected_dimensions=expected_dimensions,
                    dimension_tolerance=dimension_tolerance,
                    issue=integrity_issue,
                )
                execution_status = ExecutionStatus.ARTIFACT_REJECTED
                terminal_error_code = "geometry_input_artifact_rejected"
                terminal_error_message = integrity_issue
                runtime_provenance = None
            else:
                task = {
                    "schema_version": "mcad-capability-task.v1",
                    "capability": "geometry",
                    "operation": "validate",
                    "params": {
                        "artifacts": task_artifacts,
                        "expected_dimensions_mm": expected_dimensions,
                        "dimension_tolerance": dimension_tolerance,
                        "output": "geometry-report.json",
                    },
                    "inputs": task_inputs,
                }
                source_code = json.dumps(
                    task,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                snapshot = await asyncio.to_thread(self.backend.runtime_snapshot)
                spec = ExecutionSpec(
                    execution_attempt_id=str(attempt_id),
                    workflow_run_id=str(request.workflow_run_id),
                    step_run_id=str(step_id),
                    tenant_id=str(request.tenant_id),
                    project_id=str(request.project_id),
                    expected_base_revision_id=str(
                        request.expected_base_revision_id
                    ),
                    idempotency_key=(
                        f"agent-v2:{request.workflow_run_id}:{step_key}:"
                        f"attempt:{info.attempt}"
                    ),
                    capability="mcad.geometry",
                    operation="validate",
                    mode="analysis",
                    source=ExecutionSource(
                        language="json",
                        code=source_code,
                        sha256=hashlib.sha256(
                            source_code.encode("utf-8")
                        ).hexdigest(),
                    ),
                    inputs=tuple(declarations),
                    outputs=(
                        OutputDeclaration(
                            name="artifact",
                            media_type="application/json",
                            max_size_bytes=4 * 1024 * 1024,
                        ),
                        OutputDeclaration(
                            name="capability-result",
                            media_type="application/json",
                            max_size_bytes=4 * 1024 * 1024,
                        ),
                    ),
                    runtime=RuntimeRequirement(
                        image_digest=snapshot.image_digest,
                        platform=snapshot.platform,
                        sandbox_tier="ephemeral-job",
                    ),
                    limits=ResourceLimits(timeout_seconds=120),
                    metadata={
                        "candidate_build_id": str(candidate_build_id),
                        "staging_manifest_id": str(manifest_id),
                        "gate": "geometry",
                    },
                )
                outcome = await _run_backend_with_heartbeats(
                    self.backend,
                    spec,
                    tenant_id=request.tenant_id,
                    principal_id=request.principal_id,
                    attempt_id=attempt_id,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                    materialized_inputs=materialized,
                )
                runtime_provenance = (
                    outcome.result.provenance.model_dump(mode="json")
                    if outcome.result.provenance
                    else None
                )
                if outcome.result.status is ExecutionStatus.SUCCEEDED:
                    execution_status = ExecutionStatus.SUCCEEDED
                    report_path = outcome.files.get("artifact")
                    if report_path is None:
                        report = indeterminate_geometry_report(
                            outputs=model_outputs,
                            expected_dimensions=expected_dimensions,
                            dimension_tolerance=dimension_tolerance,
                            issue="geometry_report_missing",
                        )
                    else:
                        try:
                            report = DurableGeometryReport.model_validate_json(
                                report_path.read_text(encoding="utf-8")
                            )
                        except Exception as exc:
                            report = indeterminate_geometry_report(
                                outputs=model_outputs,
                                expected_dimensions=expected_dimensions,
                                dimension_tolerance=dimension_tolerance,
                                issue=(
                                    "geometry_report_invalid:"
                                    f"{type(exc).__name__}"
                                ),
                            )
                    result_payload = {
                        "status": "succeeded",
                        "gate": "geometry",
                        "outcome": report.outcome,
                        "report": report.model_dump(mode="json"),
                        "execution_result": outcome.result.model_dump(
                            mode="json"
                        ),
                    }
                else:
                    error = outcome.result.error
                    execution_status = outcome.result.status
                    terminal_error_code = (
                        error.code if error else "geometry_execution_failed"
                    )
                    terminal_error_message = (
                        error.message
                        if error
                        else "geometry validation execution failed"
                    )
                    report = indeterminate_geometry_report(
                        outputs=model_outputs,
                        expected_dimensions=expected_dimensions,
                        dimension_tolerance=dimension_tolerance,
                        issue=(
                            error.code if error else "geometry_execution_failed"
                        ),
                    )
            evidence = report.durable_evidence(
                runtime_provenance=runtime_provenance
            )
            recorded = await _record_agent_validation_outcome(
                payload,
                candidate_build_id=candidate_build_id,
                staging_manifest_id=manifest_id,
                gate="geometry",
                mode="required",
                outcome=report.outcome,
                evidence=evidence,
                attempt_id=attempt_id,
                step_id=step_id,
                execution_status=execution_status,
                lease_token=lease_token,
                lease_generation=lease_generation,
                result_payload=result_payload,
                error_code=terminal_error_code,
                error_message=terminal_error_message,
            )
            return {
                "status": "completed",
                "gate": "geometry",
                "mode": "required",
                "outcome": report.outcome,
                "evidence_id": str(recorded.evidence_id),
                "evidence_hash": recorded.evidence_hash,
                "report": evidence,
                "attempt_id": str(attempt_id),
                "replayed": recorded.replayed,
            }
        except asyncio.CancelledError:
            await _mark_execution_failure(
                payload,
                attempt_id=attempt_id,
                step_id=step_id,
                status=ExecutionStatus.CANCELLED,
                error_code="geometry_validation_cancelled",
                error_message="Geometry validation was cancelled.",
            )
            raise
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            if outcome is not None and outcome.work_dir is not None:
                shutil.rmtree(outcome.work_dir, ignore_errors=True)

    async def _agent_validation_input(
        self,
        *,
        request: McadAgentWorkflowV2Request,
        candidate_build_id: UUID,
        manifest_id: UUID,
        temp_dir: Path,
        preferred_format: str = "stl",
    ) -> tuple[dict[str, Any], ArtifactInput, dict[str, Path]]:
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT manifest FROM agent_staging_manifests
                        WHERE tenant_id=:tenant_id
                          AND candidate_build_id=:candidate_build_id
                          AND workflow_run_id=:workflow_id AND id=:manifest_id
                          AND status='accepted'
                        """
                    ),
                    {
                        "tenant_id": request.tenant_id,
                        "candidate_build_id": candidate_build_id,
                        "workflow_id": request.workflow_run_id,
                        "manifest_id": manifest_id,
                    },
                )
            ).mappings().one_or_none()
        if row is None:
            raise ApplicationError(
                "validation manifest is missing or not accepted",
                type="agent_validation_manifest_missing",
                non_retryable=True,
            )
        manifest = dict(row["manifest"])
        candidates = [
            dict(item)
            for item in manifest.get("outputs") or ()
            if str(item.get("format") or "").lower() in {"stl", "step"}
        ]
        if not candidates:
            raise ApplicationError(
                "visual/DFM validation requires an STL or STEP output",
                type="agent_validation_model_missing",
                non_retryable=True,
            )
        item = next(
            (value for value in candidates if value["format"] == preferred_format),
            candidates[0],
        )
        model_format = str(item["format"]).lower()
        path = temp_dir / f"validation-model.{model_format}"
        downloaded = await download_object(str(item["object_key"]), path)
        if (
            downloaded["sha256"] != str(item["sha256"])
            or downloaded["size_bytes"] != int(item["size_bytes"])
        ):
            raise ApplicationError(
                "validation input object failed integrity verification",
                type="agent_validation_input_rejected",
                non_retryable=True,
            )
        artifact_id = f"{manifest_id}:model"
        declaration = ArtifactInput(
            artifact_id=artifact_id,
            filename=path.name,
            sha256=str(item["sha256"]),
            size_bytes=int(item["size_bytes"]),
            media_type=str(item["content_type"]),
        )
        return manifest, declaration, {artifact_id: path}

    @activity.defn(name="agent_v2.render_visual")
    async def agent_render_visual(self, payload: dict[str, Any]) -> dict[str, Any]:
        info = activity.info()
        request = _agent_v2_request(payload)
        candidate_build_id = _uuid(payload, "candidate_build_id")
        manifest_id = _uuid(payload, "staging_manifest_id")
        step_key = str(payload["validation_step_key"])
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            replay = await get_validation_evidence_for_manifest(
                connection,
                tenant_id=request.tenant_id,
                candidate_build_id=candidate_build_id,
                workflow_id=request.workflow_run_id,
                staging_manifest_id=manifest_id,
                gate="visual",
            )
            if replay is not None:
                return {
                    "status": "completed",
                    "outcome": replay["outcome"],
                    "evidence_id": str(replay["id"]),
                    "report": dict(replay["evidence"]),
                    "attempt_id": str(replay["execution_attempt_id"]),
                    "replayed": True,
                }
            completed = await _stored_validation_attempt_result(
                connection,
                workflow_id=request.workflow_run_id,
                step_key=step_key,
            )
            if completed is not None:
                return completed
        attempt_id, step_id, lease_token, lease_generation = (
            await _prepare_agent_validation_attempt(
                payload,
                temporal_attempt=info.attempt,
                step_key=step_key,
                step_index=int(payload["step_index"]),
                step_kind="agent_visual_render",
            )
        )
        temp_dir = Path(tempfile.mkdtemp(prefix="agent_visual_"))
        outcome: MaterializedExecutionOutcome | None = None
        try:
            try:
                _, declaration, materialized = await self._agent_validation_input(
                    request=request,
                    candidate_build_id=candidate_build_id,
                    manifest_id=manifest_id,
                    temp_dir=temp_dir,
                )
            except Exception as exc:
                report = indeterminate_visual_report(
                    issue=f"visual_input_unavailable:{type(exc).__name__}"
                )
                recorded = await _record_agent_validation_outcome(
                    payload,
                    candidate_build_id=candidate_build_id,
                    staging_manifest_id=manifest_id,
                    gate="visual",
                    mode=str(payload["gate_mode"]),
                    outcome="indeterminate",
                    evidence=report.durable_evidence(),
                    attempt_id=attempt_id,
                    step_id=step_id,
                    execution_status=ExecutionStatus.ARTIFACT_REJECTED,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                    error_code="visual_input_rejected",
                    error_message=str(exc)[:4000],
                )
                return {
                    "status": "completed",
                    "outcome": "indeterminate",
                    "evidence_id": str(recorded.evidence_id),
                    "report": report.durable_evidence(),
                    "attempt_id": str(attempt_id),
                }
            task = {
                "schema_version": "mcad-capability-task.v1",
                "capability": "visual",
                "operation": "render",
                "params": {"width": 512, "height": 512},
                "inputs": {"model": declaration.filename},
            }
            source_code = json.dumps(
                task, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            snapshot = await asyncio.to_thread(self.backend.runtime_snapshot)
            spec = ExecutionSpec(
                execution_attempt_id=str(attempt_id),
                workflow_run_id=str(request.workflow_run_id),
                step_run_id=str(step_id),
                tenant_id=str(request.tenant_id),
                project_id=str(request.project_id),
                expected_base_revision_id=str(request.expected_base_revision_id),
                idempotency_key=f"agent-v2:{request.workflow_run_id}:{step_key}:{info.attempt}",
                capability="mcad.visual",
                operation="render",
                mode="analysis",
                source=ExecutionSource(
                    language="json",
                    code=source_code,
                    sha256=hashlib.sha256(source_code.encode()).hexdigest(),
                ),
                inputs=(declaration,),
                outputs=tuple(
                    [
                        OutputDeclaration(
                            name=view,
                            media_type="image/png",
                            max_size_bytes=4 * 1024 * 1024,
                        )
                        for view in ("front", "right", "top", "isometric")
                    ]
                    + [
                        OutputDeclaration(
                            name="capability-result",
                            media_type="application/json",
                            max_size_bytes=512 * 1024,
                        )
                    ]
                ),
                runtime=RuntimeRequirement(
                    image_digest=snapshot.image_digest,
                    platform=snapshot.platform,
                    sandbox_tier="ephemeral-job",
                ),
                limits=ResourceLimits(
                    timeout_seconds=120,
                    memory_bytes=1536 * 1024 * 1024,
                    pids=512,
                ),
                metadata={
                    "candidate_build_id": str(candidate_build_id),
                    "staging_manifest_id": str(manifest_id),
                    "gate": "visual",
                },
            )
            outcome = await _run_backend_with_heartbeats(
                self.backend,
                spec,
                tenant_id=request.tenant_id,
                principal_id=request.principal_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                lease_generation=lease_generation,
                materialized_inputs=materialized,
            )
            runtime_provenance = (
                outcome.result.provenance.model_dump(mode="json")
                if outcome.result.provenance
                else None
            )
            if outcome.result.status is not ExecutionStatus.SUCCEEDED:
                error = outcome.result.error
                report = indeterminate_visual_report(
                    issue=error.code if error else "visual_render_failed",
                    runtime_provenance=runtime_provenance,
                )
                recorded = await _record_agent_validation_outcome(
                    payload,
                    candidate_build_id=candidate_build_id,
                    staging_manifest_id=manifest_id,
                    gate="visual",
                    mode=str(payload["gate_mode"]),
                    outcome="indeterminate",
                    evidence=report.durable_evidence(),
                    attempt_id=attempt_id,
                    step_id=step_id,
                    execution_status=outcome.result.status,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                    error_code=error.code if error else "visual_render_failed",
                    error_message=error.message if error else "render failed",
                )
                return {
                    "status": "completed",
                    "outcome": "indeterminate",
                    "evidence_id": str(recorded.evidence_id),
                    "report": report.durable_evidence(),
                    "attempt_id": str(attempt_id),
                }
            metadata_path = outcome.files.get("capability-result")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            view_facts = {
                item["view"]: item
                for item in dict(metadata["result"])["views"]
            }
            renders: list[dict[str, Any]] = []
            for view in ("front", "right", "top", "isometric"):
                path = outcome.files[view]
                declared = next(
                    item for item in outcome.result.outputs if item.name == path.name
                )
                object_key = (
                    "staging/agent/tenants/"
                    f"{request.tenant_id}/candidates/{candidate_build_id}/"
                    f"validation/{attempt_id}/{declared.sha256}/{view}.png"
                )
                uploaded = await put_file(object_key, path, content_type="image/png")
                if (
                    uploaded["sha256"] != declared.sha256
                    or uploaded["size_bytes"] != declared.size_bytes
                ):
                    raise ApplicationError(
                        "visual render upload integrity mismatch",
                        type="agent_visual_render_integrity_failed",
                        non_retryable=True,
                    )
                fact = dict(view_facts[view])
                fact["object_key"] = object_key
                renders.append(
                    VisualRenderEvidence.model_validate(fact).model_dump(mode="json")
                )
            result_payload = {
                "status": "rendered",
                "renders": renders,
                "runtime_provenance": runtime_provenance,
                "attempt_id": str(attempt_id),
                "step_id": str(step_id),
            }
            async with tenant_transaction(
                request.tenant_id,
                request.principal_id,
            ) as connection:
                await complete_attempt(
                    connection,
                    attempt_id,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                    result_payload=result_payload,
                )
                await transition_step(
                    connection,
                    step_id,
                    expected=StepStatus.RUNNING,
                    target=StepStatus.SUCCEEDED,
                )
            return result_payload
        except asyncio.CancelledError:
            await _mark_execution_failure(
                payload,
                attempt_id=attempt_id,
                step_id=step_id,
                status=ExecutionStatus.CANCELLED,
                error_code="visual_render_cancelled",
                error_message="Visual rendering was cancelled.",
            )
            raise
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            if outcome is not None and outcome.work_dir is not None:
                shutil.rmtree(outcome.work_dir, ignore_errors=True)

    @activity.defn(name="agent_v2.judge_visual")
    async def agent_judge_visual(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = _agent_v2_request(payload)
        candidate_build_id = _uuid(payload, "candidate_build_id")
        manifest_id = _uuid(payload, "staging_manifest_id")
        render_attempt_id = _uuid(payload, "render_attempt_id")
        render_step_id = _uuid(payload, "render_step_id")
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            replay = await get_validation_evidence_for_manifest(
                connection,
                tenant_id=request.tenant_id,
                candidate_build_id=candidate_build_id,
                workflow_id=request.workflow_run_id,
                staging_manifest_id=manifest_id,
                gate="visual",
            )
            if replay is not None:
                return {
                    "status": "completed",
                    "outcome": replay["outcome"],
                    "evidence_id": str(replay["id"]),
                    "report": dict(replay["evidence"]),
                    "attempt_id": str(replay["execution_attempt_id"]),
                    "replayed": True,
                }
        temp_dir = Path(tempfile.mkdtemp(prefix="agent_vision_provider_"))
        render_models = tuple(
            VisualRenderEvidence.model_validate(item)
            for item in payload.get("renders") or ()
        )
        try:
            paths: list[Path] = []
            for item in render_models:
                path = temp_dir / item.filename
                downloaded = await download_object(item.object_key, path)
                if (
                    downloaded["sha256"] != item.sha256
                    or downloaded["size_bytes"] != item.size_bytes
                ):
                    raise ValueError("visual render object integrity mismatch")
                paths.append(path)
            report = await self.durable_visual.report(
                objective=str(payload["objective"]),
                design_brief=dict(payload["design_brief"]),
                render_paths=tuple(paths),
                renders=render_models,
                runtime_provenance=dict(payload["runtime_provenance"]),
            )
            evidence = report.durable_evidence()
            async with tenant_transaction(
                request.tenant_id,
                request.principal_id,
            ) as connection:
                recorded = await record_validation_evidence(
                    connection,
                    tenant_id=request.tenant_id,
                    candidate_build_id=candidate_build_id,
                    workflow_id=request.workflow_run_id,
                    step_id=render_step_id,
                    attempt_id=render_attempt_id,
                    staging_manifest_id=manifest_id,
                    gate="visual",
                    mode=str(payload["gate_mode"]),
                    outcome=report.outcome,
                    evidence=evidence,
                )
            return {
                "status": "completed",
                "outcome": report.outcome,
                "evidence_id": str(recorded.evidence_id),
                "evidence_hash": recorded.evidence_hash,
                "report": evidence,
                "attempt_id": str(render_attempt_id),
                "replayed": recorded.replayed,
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @activity.defn(name="agent_v2.repair_visual")
    async def agent_repair_visual(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = _agent_v2_request(payload)
        candidate_build_id = _uuid(payload, "candidate_build_id")
        source_id = _uuid(payload, "source_id")
        step_key = str(payload["repair_step_key"])
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            replay = await get_generated_source_for_step(
                connection,
                tenant_id=request.tenant_id,
                workflow_id=request.workflow_run_id,
                step_key=step_key,
            )
            if replay is not None:
                return {
                    "source_id": str(replay["id"]),
                    "source_hash": replay["source_hash"],
                    "source_code": replay["source_code"],
                    "repair_step_key": step_key,
                    "replayed": True,
                }
            source = (
                await connection.execute(
                    text(
                        """
                        SELECT source_code, source_hash FROM agent_generated_sources
                        WHERE tenant_id=:tenant_id AND id=:source_id
                          AND candidate_build_id=:candidate_build_id
                        """
                    ),
                    {
                        "tenant_id": request.tenant_id,
                        "source_id": source_id,
                        "candidate_build_id": candidate_build_id,
                    },
                )
            ).mappings().one_or_none()
            if source is None or source["source_hash"] != str(payload["source_hash"]):
                raise ApplicationError(
                    "visual repair source is missing or changed",
                    type="agent_visual_repair_source_conflict",
                    non_retryable=True,
                )
            step_id = await _start_agent_logical_step(
                connection,
                tenant_id=request.tenant_id,
                workflow_id=request.workflow_run_id,
                step_key=step_key,
                step_index=int(payload["step_index"]),
                kind="agent_visual_repair",
            )
        try:
            repaired, provenance = await self.durable_visual.repair(
                source_code=str(source["source_code"]),
                issues=tuple(str(item) for item in payload.get("issues") or ()),
                suggestions=tuple(
                    str(item) for item in payload.get("suggestions") or ()
                ),
            )
            async with tenant_transaction(
                request.tenant_id,
                request.principal_id,
            ) as connection:
                recorded = await record_generated_source(
                    connection,
                    tenant_id=request.tenant_id,
                    candidate_build_id=candidate_build_id,
                    workflow_id=request.workflow_run_id,
                    step_id=step_id,
                    predecessor_source_id=source_id,
                    source_code=repaired,
                    generator_kind="repair:vision_mismatch",
                    provider=str(provenance["provider"]),
                    model=str(provenance["model"]),
                    provider_response_id=(
                        str(provenance["provider_response_id"])
                        if provenance.get("provider_response_id")
                        else None
                    ),
                    request_hash=str(provenance["request_hash"]),
                    response_hash=str(provenance["response_hash"]),
                    finish_reason=(
                        str(provenance["finish_reason"])
                        if provenance.get("finish_reason")
                        else None
                    ),
                    usage=dict(provenance.get("usage") or {}),
                )
                await append_workflow_event(
                    connection,
                    tenant_id=request.tenant_id,
                    workflow_id=request.workflow_run_id,
                    event_type="agent.visual_repair.source_generated",
                    payload={
                        "repair_step_key": step_key,
                        "prior_source_id": str(source_id),
                        "source_id": str(recorded.source_id),
                        "prior_source_hash": str(source["source_hash"]),
                        "source_hash": recorded.source_hash,
                    },
                )
            return {
                "source_id": str(recorded.source_id),
                "source_hash": recorded.source_hash,
                "source_code": repaired,
                "repair_step_key": step_key,
                "provenance": provenance,
                "replayed": recorded.replayed,
            }
        except Exception as exc:
            await _fail_agent_logical_step(
                tenant_id=request.tenant_id,
                principal_id=request.principal_id,
                workflow_id=request.workflow_run_id,
                step_key=step_key,
                error_code="agent_visual_repair_failed",
                error_message=str(exc)[:4000],
            )
            raise

    @activity.defn(name="agent_v2.validate_dfm")
    async def agent_validate_dfm(self, payload: dict[str, Any]) -> dict[str, Any]:
        info = activity.info()
        request = _agent_v2_request(payload)
        candidate_build_id = _uuid(payload, "candidate_build_id")
        manifest_id = _uuid(payload, "staging_manifest_id")
        step_key = str(payload["validation_step_key"])
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            replay = await get_validation_evidence_for_manifest(
                connection,
                tenant_id=request.tenant_id,
                candidate_build_id=candidate_build_id,
                workflow_id=request.workflow_run_id,
                staging_manifest_id=manifest_id,
                gate="dfm",
            )
            if replay is not None:
                return {
                    "status": "completed",
                    "outcome": replay["outcome"],
                    "evidence_id": str(replay["id"]),
                    "evidence_hash": replay["evidence_hash"],
                    "report": dict(replay["evidence"]),
                    "attempt_id": str(replay["execution_attempt_id"]),
                    "replayed": True,
                }
            policy = await resolve_dfm_policy_snapshot(
                connection,
                tenant_id=request.tenant_id,
                manufacturing_profile=(
                    dict(request.manufacturing_profile)
                    if request.manufacturing_profile
                    else None
                ),
            )
        attempt_id, step_id, lease_token, lease_generation = (
            await _prepare_agent_validation_attempt(
                payload,
                temporal_attempt=info.attempt,
                step_key=step_key,
                step_index=int(payload["step_index"]),
                step_kind="agent_dfm_validation",
            )
        )
        temp_dir = Path(tempfile.mkdtemp(prefix="agent_dfm_"))
        outcome: MaterializedExecutionOutcome | None = None
        try:
            _, declaration, materialized = await self._agent_validation_input(
                request=request,
                candidate_build_id=candidate_build_id,
                manifest_id=manifest_id,
                temp_dir=temp_dir,
                preferred_format="step",
            )
            policy_path = temp_dir / "dfm-policy.json"
            policy_path.write_bytes(policy.canonical_bytes())
            policy_id = f"{manifest_id}:dfm-policy:{policy.policy_hash}"
            policy_declaration = ArtifactInput(
                artifact_id=policy_id,
                filename=policy_path.name,
                sha256=policy.policy_hash,
                size_bytes=policy_path.stat().st_size,
                media_type="application/json",
            )
            materialized[policy_id] = policy_path
            task = {
                "schema_version": "mcad-capability-task.v1",
                "capability": "dfm",
                "operation": "validate",
                "params": {
                    "policy_hash": policy.policy_hash,
                    "output": "dfm-report.json",
                },
                "inputs": {
                    "model": declaration.filename,
                    "policy": policy_declaration.filename,
                },
            }
            source_code = json.dumps(
                task, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            snapshot = await asyncio.to_thread(self.backend.runtime_snapshot)
            spec = ExecutionSpec(
                execution_attempt_id=str(attempt_id),
                workflow_run_id=str(request.workflow_run_id),
                step_run_id=str(step_id),
                tenant_id=str(request.tenant_id),
                project_id=str(request.project_id),
                expected_base_revision_id=str(request.expected_base_revision_id),
                idempotency_key=f"agent-v2:{request.workflow_run_id}:{step_key}:{info.attempt}",
                capability="mcad.dfm",
                operation="validate",
                mode="analysis",
                source=ExecutionSource(
                    language="json",
                    code=source_code,
                    sha256=hashlib.sha256(source_code.encode()).hexdigest(),
                ),
                inputs=(declaration, policy_declaration),
                outputs=(
                    OutputDeclaration(
                        name="artifact",
                        media_type="application/json",
                        max_size_bytes=4 * 1024 * 1024,
                    ),
                    OutputDeclaration(
                        name="capability-result",
                        media_type="application/json",
                        max_size_bytes=4 * 1024 * 1024,
                    ),
                ),
                runtime=RuntimeRequirement(
                    image_digest=snapshot.image_digest,
                    platform=snapshot.platform,
                    sandbox_tier="ephemeral-job",
                ),
                limits=ResourceLimits(timeout_seconds=120),
                metadata={
                    "candidate_build_id": str(candidate_build_id),
                    "staging_manifest_id": str(manifest_id),
                    "gate": "dfm",
                    "policy_hash": policy.policy_hash,
                },
            )
            outcome = await _run_backend_with_heartbeats(
                self.backend,
                spec,
                tenant_id=request.tenant_id,
                principal_id=request.principal_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                lease_generation=lease_generation,
                materialized_inputs=materialized,
            )
            runtime_provenance = (
                outcome.result.provenance.model_dump(mode="json")
                if outcome.result.provenance
                else None
            )
            result_payload = None
            report_artifact: dict[str, Any] | None = None
            if outcome.result.status is ExecutionStatus.SUCCEEDED:
                try:
                    report_path = outcome.files["artifact"]
                    report = DurableDFMReport.model_validate_json(
                        report_path.read_text(encoding="utf-8")
                    )
                    if report.policy_hash != policy.policy_hash:
                        raise ValueError("DFM report used another policy")
                    report_digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
                    report_key = (
                        "staging/agent/tenants/"
                        f"{request.tenant_id}/candidates/{candidate_build_id}/"
                        f"validation/{attempt_id}/{report_digest}/dfm-report.json"
                    )
                    uploaded = await put_file(
                        report_key,
                        report_path,
                        content_type="application/json",
                    )
                    report_artifact = {
                        "filename": "dfm-report.json",
                        "object_key": report_key,
                        "sha256": uploaded["sha256"],
                        "size_bytes": uploaded["size_bytes"],
                        "content_type": "application/json",
                    }
                except Exception as exc:
                    report = indeterminate_dfm_report(
                        process=policy.process,
                        material=policy.material,
                        policy_hash=policy.policy_hash,
                        issue=f"dfm_report_invalid:{type(exc).__name__}",
                    )
                execution_status = ExecutionStatus.SUCCEEDED
                result_payload = {
                    "status": "succeeded",
                    "gate": "dfm",
                    "outcome": report.outcome,
                    "report": report.model_dump(mode="json"),
                    "execution_result": outcome.result.model_dump(mode="json"),
                }
                error_code = error_message = None
            else:
                error = outcome.result.error
                report = indeterminate_dfm_report(
                    process=policy.process,
                    material=policy.material,
                    policy_hash=policy.policy_hash,
                    issue=error.code if error else "dfm_execution_failed",
                )
                execution_status = outcome.result.status
                error_code = error.code if error else "dfm_execution_failed"
                error_message = error.message if error else "DFM execution failed"
            evidence = report.durable_evidence(
                runtime_provenance=runtime_provenance,
                policy_object={
                    **policy.model_dump(mode="json"),
                    "policy_hash": policy.policy_hash,
                },
            )
            if report_artifact is not None:
                evidence["report_artifact"] = report_artifact
            recorded = await _record_agent_validation_outcome(
                payload,
                candidate_build_id=candidate_build_id,
                staging_manifest_id=manifest_id,
                gate="dfm",
                mode=str(payload["gate_mode"]),
                outcome=report.outcome,
                evidence=evidence,
                attempt_id=attempt_id,
                step_id=step_id,
                execution_status=execution_status,
                lease_token=lease_token,
                lease_generation=lease_generation,
                result_payload=result_payload,
                error_code=error_code,
                error_message=error_message,
            )
            return {
                "status": "completed",
                "outcome": report.outcome,
                "evidence_id": str(recorded.evidence_id),
                "evidence_hash": recorded.evidence_hash,
                "report": evidence,
                "attempt_id": str(attempt_id),
                "replayed": recorded.replayed,
            }
        except asyncio.CancelledError:
            await _mark_execution_failure(
                payload,
                attempt_id=attempt_id,
                step_id=step_id,
                status=ExecutionStatus.CANCELLED,
                error_code="dfm_validation_cancelled",
                error_message="DFM validation was cancelled.",
            )
            raise
        except Exception as exc:
            report = indeterminate_dfm_report(
                process=policy.process,
                material=policy.material,
                policy_hash=policy.policy_hash,
                issue=f"dfm_unavailable:{type(exc).__name__}",
            )
            evidence = report.durable_evidence(
                runtime_provenance=None,
                policy_object={
                    **policy.model_dump(mode="json"),
                    "policy_hash": policy.policy_hash,
                },
            )
            recorded = await _record_agent_validation_outcome(
                payload,
                candidate_build_id=candidate_build_id,
                staging_manifest_id=manifest_id,
                gate="dfm",
                mode=str(payload["gate_mode"]),
                outcome="indeterminate",
                evidence=evidence,
                attempt_id=attempt_id,
                step_id=step_id,
                execution_status=ExecutionStatus.FAILED,
                lease_token=lease_token,
                lease_generation=lease_generation,
                error_code="dfm_validation_failed",
                error_message=str(exc)[:4000],
            )
            return {
                "status": "completed",
                "outcome": "indeterminate",
                "evidence_id": str(recorded.evidence_id),
                "evidence_hash": recorded.evidence_hash,
                "report": evidence,
                "attempt_id": str(attempt_id),
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            if outcome is not None and outcome.work_dir is not None:
                shutil.rmtree(outcome.work_dir, ignore_errors=True)

    @activity.defn(name="agent_v2.generate_bom")
    async def agent_generate_bom(self, payload: dict[str, Any]) -> dict[str, Any]:
        info = activity.info()
        request = _agent_v2_request(payload)
        plan = AgentPlan.model_validate(payload["plan"])
        candidate_build_id = _uuid(payload, "candidate_build_id")
        step_key = "agent-native-bom"
        attempt_id, step_id, lease_token, lease_generation = (
            await _prepare_agent_validation_attempt(
                payload,
                temporal_attempt=info.attempt,
                step_key=step_key,
                step_index=60_000,
                step_kind="agent_bom",
            )
        )

        async def fail_preflight(
            *,
            code: str,
            message: str,
            category: str,
            retryable: bool = False,
        ) -> None:
            error = ExecutionError(
                category=category,
                code=code,
                message=message,
                retryable=retryable,
            )
            await _mark_execution_failure(
                payload,
                attempt_id=attempt_id,
                step_id=step_id,
                status=ExecutionStatus.FAILED,
                error_code=error.code,
                error_message=error.message,
                error=error,
            )

        if plan.model_kind != "assembly" or plan.validation_policy.bom.mode.value != "required":
            await fail_preflight(
                code="bom_source_not_assembly",
                message="native BOM is valid only for required assembly plans",
                category="validation",
            )
            raise ApplicationError(
                "native BOM is valid only for required assembly plans",
                type="bom_source_not_assembly",
                non_retryable=True,
            )
        try:
            async with tenant_transaction(
                request.tenant_id,
                request.principal_id,
            ) as connection:
                rows = [
                    dict(row)
                    for row in (
                        await connection.execute(
                            text(
                                """
                                SELECT m.id, s.step_key,
                                       m.execution_attempt_id,
                                       m.manifest, m.manifest_hash
                                FROM agent_staging_manifests AS m
                                JOIN step_runs AS s
                                  ON s.tenant_id=m.tenant_id
                                 AND s.workflow_run_id=m.workflow_run_id
                                 AND s.id=m.step_run_id
                                WHERE m.tenant_id=:tenant_id
                                  AND m.candidate_build_id=:candidate_build_id
                                  AND m.workflow_run_id=:workflow_id
                                  AND m.status='accepted'
                                ORDER BY m.created_at, m.id
                                """
                            ),
                            {
                                "tenant_id": request.tenant_id,
                                "candidate_build_id": candidate_build_id,
                                "workflow_id": request.workflow_run_id,
                            },
                        )
                    ).mappings().all()
                ]
        except Exception as exc:
            await fail_preflight(
                code="bom_input_query_failed",
                message="assembly BOM inputs could not be loaded",
                category="infrastructure",
                retryable=True,
            )
            raise ApplicationError(
                "assembly BOM inputs could not be loaded",
                type="bom_input_query_failed",
            ) from exc
        by_step: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_step.setdefault(str(row["step_key"]), []).append(row)
        combine_step = next(
            step for step in plan.steps if step.kind == "assembly_combine"
        )
        part_steps = [step for step in plan.steps if step.kind == "assembly_part"]
        required_steps = [combine_step.step_key, *(step.step_key for step in part_steps)]
        if any(len(by_step.get(step_key, ())) != 1 for step_key in required_steps):
            error_code = (
                "bom_input_ambiguous"
                if any(len(by_step.get(key, ())) > 1 for key in required_steps)
                else "bom_input_missing"
            )
            await fail_preflight(
                code=error_code,
                message="assembly BOM inputs are missing or ambiguous",
                category="artifact",
            )
            raise ApplicationError(
                "assembly BOM inputs are missing or ambiguous",
                type=error_code,
                non_retryable=True,
            )
        selected = {key: by_step[key][0] for key in required_steps}
        temp_dir = Path(tempfile.mkdtemp(prefix="agent_bom_"))
        outcome: MaterializedExecutionOutcome | None = None
        try:
            declarations: list[ArtifactInput] = []
            materialized: dict[str, Path] = {}
            task_inputs: dict[str, str] = {}
            source_artifacts: dict[str, dict[str, Any]] = {}
            for step in (combine_step, *part_steps):
                row = selected[step.step_key]
                manifest = dict(row["manifest"])
                step_outputs = [
                    dict(item)
                    for item in manifest.get("outputs") or ()
                    if str(item.get("format") or "").lower() == "step"
                ]
                if len(step_outputs) != 1:
                    raise ApplicationError(
                        "assembly BOM requires one STEP per plan step",
                        type=(
                            "bom_input_ambiguous"
                            if len(step_outputs) > 1
                            else "bom_input_missing"
                        ),
                        non_retryable=True,
                    )
                output = step_outputs[0]
                role = (
                    "assembly"
                    if step.kind == "assembly_combine"
                    else f"component:{step.step_key}"
                )
                filename = (
                    "combine.step"
                    if step.kind == "assembly_combine"
                    else f"{step.step_key}.step"
                )
                local_path = temp_dir / filename
                downloaded = await download_object(
                    str(output["object_key"]),
                    local_path,
                )
                if (
                    downloaded["sha256"] != str(output["sha256"])
                    or downloaded["size_bytes"] != int(output["size_bytes"])
                ):
                    raise ApplicationError(
                        "assembly BOM input failed integrity verification",
                        type="bom_input_integrity_failed",
                        non_retryable=True,
                    )
                artifact_id = role
                declarations.append(ArtifactInput(
                    artifact_id=artifact_id,
                    filename=filename,
                    sha256=str(output["sha256"]),
                    size_bytes=int(output["size_bytes"]),
                    media_type=str(output["content_type"]),
                ))
                materialized[artifact_id] = local_path
                task_inputs[role] = filename
                source_artifacts[step.step_key] = {
                    "step_key": step.step_key,
                    "staging_manifest_id": str(row["id"]),
                    "artifact_role": "step",
                    "filename": filename,
                    "sha256": str(output["sha256"]),
                }
            snapshot = await asyncio.to_thread(self.backend.runtime_snapshot)
            bom_request = FreeCADBOMRequestV1(
                candidate_build_id=candidate_build_id,
                base_revision_id=request.expected_base_revision_id,
                plan_hash=canonical_sha256(plan.temporal_payload()),
                runtime_image_digest=snapshot.image_digest,
                combine_step_key=combine_step.step_key,
                components=tuple({
                    "step_key": step.step_key,
                    "label": step.part_name,
                    "position_mm": step.part_position,
                    "artifact_id": f"component:{step.step_key}",
                    "quantity": 1,
                } for step in part_steps),
                property_columns=(),
            )
            task = {
                "schema_version": "mcad-capability-task.v1",
                "capability": "freecad",
                "operation": "bom",
                "params": {"request": bom_request.model_dump(mode="json")},
                "inputs": task_inputs,
            }
            source_code = json.dumps(
                task,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            spec = ExecutionSpec(
                execution_attempt_id=str(attempt_id),
                workflow_run_id=str(request.workflow_run_id),
                step_run_id=str(step_id),
                tenant_id=str(request.tenant_id),
                project_id=str(request.project_id),
                expected_base_revision_id=str(request.expected_base_revision_id),
                idempotency_key=(
                    f"agent-v2:bom:{request.workflow_run_id}:attempt:{info.attempt}"
                ),
                capability="mcad.freecad",
                operation="bom",
                mode="analysis",
                source=ExecutionSource(
                    language="json",
                    code=source_code,
                    sha256=hashlib.sha256(source_code.encode()).hexdigest(),
                ),
                inputs=tuple(declarations),
                outputs=(
                    OutputDeclaration(
                        name="bom-json",
                        media_type="application/json",
                        max_size_bytes=16 * 1024 * 1024,
                    ),
                    OutputDeclaration(
                        name="bom-csv",
                        media_type="text/csv; charset=utf-8",
                        max_size_bytes=16 * 1024 * 1024,
                    ),
                    OutputDeclaration(
                        name="capability-result",
                        media_type="application/json",
                        max_size_bytes=512 * 1024,
                    ),
                ),
                runtime=RuntimeRequirement(
                    image_digest=snapshot.image_digest,
                    platform=snapshot.platform,
                    sandbox_tier="ephemeral-job",
                ),
                limits=ResourceLimits(
                    timeout_seconds=180,
                    memory_bytes=1536 * 1024 * 1024,
                    cpu_millis=2000,
                    pids=512,
                    output_bytes=384 * 1024 * 1024,
                ),
                metadata={
                    "candidate_build_id": str(candidate_build_id),
                    "gate": "bom",
                },
            )
            outcome = await _run_backend_with_heartbeats(
                self.backend,
                spec,
                tenant_id=request.tenant_id,
                principal_id=request.principal_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                lease_generation=lease_generation,
                materialized_inputs=materialized,
            )
            if outcome.result.status is not ExecutionStatus.SUCCEEDED:
                error = outcome.result.error or ExecutionError(
                    category="cad_kernel",
                    code="bom_generation_failed",
                    message="native Assembly BOM execution failed",
                )
                await _mark_execution_failure(
                    payload,
                    attempt_id=attempt_id,
                    step_id=step_id,
                    status=outcome.result.status,
                    error_code=error.code,
                    error_message=error.message,
                    error=error,
                )
                raise ApplicationError(
                    error.message,
                    type=error.code,
                    non_retryable=not error.retryable,
                )
            runner_path = outcome.files["bom-json"]
            runner = json.loads(runner_path.read_text(encoding="utf-8"))
            if (
                runner.get("schema_version") != "freecad-bom-runner.v1"
                or runner.get("generator", {}).get("runtime_image_digest")
                != snapshot.image_digest
            ):
                raise ApplicationError(
                    "native BOM result provenance is invalid",
                    type="bom_generation_failed",
                    non_retryable=True,
                )
            bom_document = FreeCADBOMDocumentV1(
                source={
                    "candidate_build_id": str(candidate_build_id),
                    "base_revision_id": str(request.expected_base_revision_id),
                    "plan_hash": canonical_sha256(plan.temporal_payload()),
                    "combine": {
                        key: value
                        for key, value in source_artifacts[combine_step.step_key].items()
                        if key != "step_key"
                    },
                    "components": [
                        source_artifacts[step.step_key] for step in part_steps
                    ],
                },
                generator=dict(runner["generator"]),
                columns=tuple(runner["columns"]),
                rows=tuple(runner["rows"]),
            )
            runner_path.write_text(
                json.dumps(
                    bom_document.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            bom_artifacts = []
            for role, filename, content_type in (
                ("bom-json", "bom.json", "application/json"),
                ("bom-csv", "bom.csv", "text/csv; charset=utf-8"),
            ):
                path = outcome.files[role]
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                object_key = (
                    "staging/agent/tenants/"
                    f"{request.tenant_id}/candidates/{candidate_build_id}/"
                    f"validation/{attempt_id}/{digest}/{filename}"
                )
                uploaded = await put_file(
                    object_key,
                    path,
                    content_type=content_type,
                )
                bom_artifacts.append({
                    "role": role,
                    "filename": filename,
                    "object_key": object_key,
                    "sha256": uploaded["sha256"],
                    "size_bytes": uploaded["size_bytes"],
                    "content_type": content_type,
                })
            evidence = {
                "schema_version": "agent-bom-evidence.v1",
                "source_manifest_id": str(selected[combine_step.step_key]["id"]),
                "artifacts": bom_artifacts,
                "runtime_provenance": (
                    outcome.result.provenance.model_dump(mode="json")
                    if outcome.result.provenance
                    else None
                ),
            }
            recorded = await _record_agent_validation_outcome(
                payload,
                candidate_build_id=candidate_build_id,
                staging_manifest_id=UUID(str(selected[combine_step.step_key]["id"])),
                gate="bom",
                mode="required",
                outcome="passed",
                evidence=evidence,
                attempt_id=attempt_id,
                step_id=step_id,
                execution_status=ExecutionStatus.SUCCEEDED,
                lease_token=lease_token,
                lease_generation=lease_generation,
                result_payload={
                    "status": "succeeded",
                    "gate": "bom",
                    "outcome": "passed",
                    "document": bom_document.model_dump(mode="json"),
                },
                error_code=None,
                error_message=None,
            )
            return {
                "status": "succeeded",
                "outcome": "passed",
                "evidence_id": str(recorded.evidence_id),
                "evidence_hash": recorded.evidence_hash,
            }
        except ApplicationError as exc:
            code = str(exc.type or "bom_generation_failed")
            category = (
                "artifact"
                if code.startswith("bom_input_")
                else "infrastructure"
                if code == "bom_runtime_unsupported"
                else "validation"
                if code in {
                    "bom_source_not_assembly",
                    "bom_source_hierarchy_lost",
                    "bom_source_geometry_mismatch",
                    "bom_empty",
                }
                else "cad_kernel"
            )
            error = ExecutionError(
                category=category,
                code=code,
                message=str(exc)[:4000],
            )
            await _mark_execution_failure(
                payload,
                attempt_id=attempt_id,
                step_id=step_id,
                status=ExecutionStatus.FAILED,
                error_code=error.code,
                error_message=error.message,
                error=error,
            )
            raise
        except Exception as exc:
            error = ExecutionError(
                category="cad_kernel",
                code="bom_generation_failed",
                message="native Assembly BOM generation failed",
                evidence={"runtime_error_type": type(exc).__name__},
            )
            await _mark_execution_failure(
                payload,
                attempt_id=attempt_id,
                step_id=step_id,
                status=ExecutionStatus.FAILED,
                error_code=error.code,
                error_message=error.message,
                error=error,
            )
            raise ApplicationError(
                error.message,
                type=error.code,
                non_retryable=True,
            ) from exc
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            if outcome is not None and outcome.work_dir is not None:
                shutil.rmtree(outcome.work_dir, ignore_errors=True)

    @activity.defn(name="agent_v2.seal_candidate")
    async def agent_seal_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = _agent_v2_request(payload)
        plan = AgentPlan.model_validate(payload["plan"])
        sealed = await seal_agent_candidate(
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            workflow_id=request.workflow_run_id,
            candidate_build_id=_uuid(payload, "candidate_build_id"),
            plan=plan.temporal_payload(),
            selected_manifests=[
                dict(item) for item in payload.get("selected_manifests") or ()
            ],
        )
        return {
            "status": "succeeded",
            "seal_id": str(sealed.seal_id),
            "candidate_build_id": str(sealed.candidate_build_id),
            "candidate_revision_id": str(sealed.candidate_revision_id),
            "change_set_id": str(sealed.change_set_id),
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
                for item in sealed.artifacts
            ],
            "replayed": sealed.replayed,
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
        input_directory = None
        try:
            inputs, materialized = (), None
            if payload.get("input_artifacts"):
                from app.workflows.engineering_activities import materialize_engineering_input
                input_directory = tempfile.TemporaryDirectory(prefix="cad-engineering-input-")
                inputs, materialized = await materialize_engineering_input(payload, input_directory.name)
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
                inputs=inputs,
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
                    timeout_seconds=int(execution["timeout_seconds"]),
                    **({"memory_bytes": 1024 * 1024 * 1024, "pids": 256,
                        "cpu_millis": 2000, "output_bytes": 128 * 1024 * 1024} if inputs else {}),
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
                materialized_inputs=materialized,
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
                if payload.get("engineering_task"):
                    # Several analyses can reference one immutable revision;
                    # each workflow must own distinct revision filenames.
                    filename = f"{workflow_id}-{filename}"
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
        except ApplicationError as exc:
            if input_directory is not None and outcome is None:
                await _mark_execution_failure(payload, attempt_id=attempt_id, step_id=step_id,
                    status=ExecutionStatus.FAILED, error_code=exc.type or "engineering_input_failed",
                    error_message=str(exc)[:4000])
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
            if input_directory is not None:
                input_directory.cleanup()
            if outcome and outcome.work_dir:
                await asyncio.to_thread(
                    shutil.rmtree,
                    outcome.work_dir,
                    True,
                )

    @activity.defn(name="engineering.compute")
    async def engineering_compute(self, payload: dict[str, Any]) -> dict[str, Any]:
        from app.workflows.engineering_activities import compute_engineering
        return await compute_engineering(self, payload)

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

    @activity.defn(name="document.acquire")
    async def acquire_document(self, payload: dict) -> dict:
        from app.services.cloud_documents import acquire_operation
        return await acquire_operation(UUID(payload["tenant_id"]), UUID(payload["principal_id"]), UUID(payload["workflow_run_id"]))

    @activity.defn(name="document.release")
    async def release_document(self, payload: dict) -> dict:
        from app.services.cloud_documents import finish_operation
        return await finish_operation(UUID(payload["tenant_id"]), UUID(payload["principal_id"]), UUID(payload["workflow_run_id"]))

    def registered(self) -> list:
        return [
            self.acquire_document,
            self.release_document,
            self.prepare_source,
            self.plan,
            self.execute,
            self.check,
            self.engineering_compute,
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
            self.acquire_document,
            self.release_document,
            self.agent_requirements,
            self.agent_decompose,
            self.agent_plan,
            self.agent_allocate_candidate,
            self.agent_terminate_candidate,
            self.agent_generate_source,
            self.agent_generate_operations,
            self.agent_repair_source,
            self.agent_repair_operations,
            self.agent_execute_model,
            self.agent_execute_freecad,
            self.agent_validate_geometry,
            self.agent_render_visual,
            self.agent_judge_visual,
            self.agent_repair_visual,
            self.agent_validate_dfm,
            self.agent_generate_bom,
            self.agent_seal_candidate,
            self.wait_confirmation,
            self.resume_after_confirmation,
            self.record_cancel,
            self.record_timeout,
            self.record_failure,
        ]
