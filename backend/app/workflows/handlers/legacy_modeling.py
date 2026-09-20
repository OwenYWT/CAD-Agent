"""Legacy modeling use cases; Temporal names live in the adapter."""
from __future__ import annotations
import asyncio
import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID
from sqlalchemy import text
from temporalio import activity
from temporalio.exceptions import ApplicationError
from app.services.public_errors import public_generation_error
from app.db import tenant_transaction
from app.domain.runs import StepStatus, WorkflowStatus
from app.execution.backend import ExecutionBackend, MaterializedExecutionOutcome
from app.execution.contracts import ExecutionSource, ExecutionSpec, ExecutionStatus, OutputDeclaration, ResourceLimits, RuntimeRequirement
from app.object_store import put_file
from app.repositories.revisions import StaleBaseRevision, create_candidate_change_set
from app.repositories.runs import append_workflow_event
from app.services.artifact_commit import authorize_artifact_upload, commit_artifacts
from app.services.run_state import create_step, transition_step, transition_workflow
from app.llm import is_nonretryable_provider_error
from app.workflows.source_preparation import SourcePreparer
from app.workflows.temporal import McadSourcePreparationRequest
from app.workflows.logical_steps import _step_row, _workflow_status, _succeed_logical_step
from app.workflows.inputs import _uuid
from app.workflows.execution_support import _stored_execution_result
from app.workflows.execution_support import _prepare_execution_attempt
from app.workflows.execution_support import _run_backend_with_heartbeats
from app.workflows.execution_support import _mark_execution_failure

async def prepare_source(payload: dict[str, Any], *, source_preparer: SourcePreparer) -> dict[str, Any]:
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
        prepared = await source_preparer.prepare(preparation)
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


async def plan(payload: dict[str, Any]) -> dict[str, Any]:
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


async def execute(payload: dict[str, Any], *, backend: ExecutionBackend) -> dict[str, Any]:
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
            input_directory = tempfile.TemporaryDirectory(prefix="cad-engineering-input-")
            if payload.get("scene_task"):
                from app.workflows.scene_activities import materialize_scene_input
                inputs, materialized = await materialize_scene_input(payload, input_directory.name)
            else:
                from app.workflows.engineering_activities import materialize_engineering_input
                inputs, materialized = await materialize_engineering_input(payload, input_directory.name)
        snapshot = await asyncio.to_thread(backend.runtime_snapshot)
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
            backend,
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
            if payload.get("engineering_task") or payload.get("scene_task"):
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
