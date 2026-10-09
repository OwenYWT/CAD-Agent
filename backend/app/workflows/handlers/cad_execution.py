"""Cad execution use cases; Temporal names live in the adapter."""
from __future__ import annotations
from app.config import settings
import asyncio
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any
from sqlalchemy import text
from temporalio import activity
from app.workflows.execution_support import execution_identity, execution_timeout
from temporalio.exceptions import ApplicationError
from app.agent.durable_plan import AgentPlan
from app.agent.durable_plan import AgentPlanStep
from app.db import tenant_transaction
from app.domain.runs import StepStatus
from app.execution.backend import ExecutionBackend, MaterializedExecutionOutcome
from app.execution.contracts import ArtifactInput, ExecutionSource, ExecutionSpec, ExecutionStatus, OutputDeclaration, ResourceLimits, RuntimeRequirement
from app.freecad.contracts import FreeCADOperationPlan
from app.contracts.cad_tools import execution_receipt
from app.object_store import download_object, put_file
from app.repositories.agent_candidates import accept_staging_manifest, get_staging_manifest_for_step
from app.services.run_state import complete_attempt, transition_step
from app.workflows.inputs import _uuid, _agent_v2_request
from app.workflows.handlers.legacy_modeling import plan
from app.workflows.execution_support import _MODELING_MEDIA_TYPES
from app.workflows.execution_support import _modeling_outputs
from app.workflows.execution_support import _prepare_agent_execution_attempt
from app.workflows.execution_support import _heartbeat_loop
from app.workflows.execution_support import _run_backend_with_heartbeats
from app.workflows.execution_support import _mark_execution_failure
from app.workflows.revision_inputs import _freecad_revision_artifact
from app.workflows.checkpoint_inputs import checkpoint_manifest, checkpoint_artifact
from app.workflows.constraint_evidence import (persist_failure_snapshot, load_repair_contract, persist_repair_verification,
    load_profile_contract, persist_profile_verification)
from app.contracts.constraint_patch import ConstraintRepairValidation

async def agent_execute_model(payload: dict[str, Any], *, backend: ExecutionBackend) -> dict[str, Any]:
    info = execution_identity()
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
        snapshot = await asyncio.to_thread(backend.runtime_snapshot)
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
            limits=ResourceLimits.from_configured_memory(settings.sandbox_memory_limit, timeout_seconds=execution_timeout(payload, 120)),
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
            backend,
            spec,
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            attempt_id=attempt_id,
            lease_token=lease_token,
            lease_generation=lease_generation,
        )
        if activity.in_activity() and activity.is_cancelled():
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
                    "operation_id": error.operation_id if error else None,
                    "action": error.action if error else None,
                    "details": dict(error.details) if error else {},
                    "evidence": dict(error.evidence) if error else {},
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


async def agent_execute_freecad(payload: dict[str, Any], *, backend: ExecutionBackend,
                              constraint_validation: ConstraintRepairValidation) -> dict[str, Any]:
    info = execution_identity()
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
        if operation_plan.execution_mode == "checkpoint" and not payload.get("checkpoint_enabled"):
            raise ValueError("checkpoint execution requires the versioned tool workflow")
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
        native_import = request.operation_context.native_import if request.operation_context else None
        if native_import and not payload.get('checkpoint_manifest_id'):
            base_path = temp_dir / ('base.FCStd' if native_import.format == 'fcstd' else 'base.step')
            downloaded = await download_object(native_import.object_key(request.tenant_id, request.branch_id), base_path)
            if downloaded['sha256'] != native_import.sha256 or downloaded['size_bytes'] != native_import.size_bytes:
                raise ValueError('导入文件完整性核验失败')
            artifact_id = str(native_import.artifact_id)
            declarations.append(ArtifactInput(artifact_id=artifact_id,filename=base_path.name,
                sha256=native_import.sha256,size_bytes=native_import.size_bytes,
                media_type='application/x-freecad' if native_import.format=='fcstd' else 'model/step'))
            materialized[artifact_id] = base_path
            task_inputs['base'] = base_path.name
        elif request.operation == "modify" or payload.get("checkpoint_manifest_id"):
            if payload.get("checkpoint_manifest_id"):
                checkpoint = await checkpoint_manifest(request, candidate_build_id, payload["checkpoint_manifest_id"])
                artifact = checkpoint_artifact(checkpoint, "fcstd")
            else:
                artifact = await _freecad_revision_artifact(request, "fcstd")
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
                if request.revision_restore else request.operation_context.source_candidate_revision_id
                if request.operation_context and request.operation_context.source_candidate_revision_id else request.expected_base_revision_id
            )
            artifact_id = f"{payload.get('checkpoint_manifest_id') or input_revision_id}:fcstd"
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

        repair_contract = await load_repair_contract(request,
            source_id=payload['source_id'], source_hash=source_hash)
        profile_contract = await load_profile_contract(request, source_id=payload['source_id'], source_hash=source_hash)
        if profile_contract:
            constraint_validation.verify_profile_contract(operation_plan, profile_contract, plan.design_brief.acceptance,
                declarations[0].sha256 if declarations else None)
        if repair_contract:
            constraint_validation.verify_contract(operation_plan, repair_contract, plan.design_brief.acceptance)
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
        elif request.operation_context and request.operation_context.source_candidate_revision_id:
            task['params']['expected_revision_id'] = str(request.operation_context.source_candidate_revision_id)
        if native_import:
            task['params']['import_format'] = native_import.format
        if repair_contract:
            checkpoint_hash = declarations[0].sha256 if declarations else None
            if checkpoint_hash != repair_contract.get('execution_checkpoint_hash', repair_contract['checkpoint_hash']):
                raise ApplicationError('constraint repair checkpoint changed before execution',
                    type='constraint_patch_rejected', non_retryable=True)
            task['params'].update(constraint_repair=repair_contract,
                repair_acceptance_hash=repair_contract['acceptance_hash'])
        if profile_contract:
            task['params'].update(profile_replan=profile_contract,
                profile_acceptance_hash=profile_contract['acceptance_hash'])
        export_formats = tuple(operation_plan.operations[-1].typed_args().formats)
        measurement_step = (operation_plan.execution_mode == 'final'
            and plan.design_brief.acceptance is not None and 'step' not in export_formats)
        if measurement_step:
            task['params']['measurement_formats'] = ['step']
        task_source = json.dumps(
            task,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
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
            + ([OutputDeclaration(name='verification_step', media_type=_MODELING_MEDIA_TYPES['step'],
                max_size_bytes=128 * 1024 * 1024)] if measurement_step else [])
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
            snapshot = await asyncio.to_thread(backend.runtime_snapshot)
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
                limits=ResourceLimits.from_configured_memory(settings.sandbox_memory_limit,
                    timeout_seconds=execution_timeout(payload, 180),
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
                backend,
                spec,
                tenant_id=request.tenant_id,
                principal_id=request.principal_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                lease_generation=lease_generation,
                materialized_inputs=materialized,
            )
            if activity.in_activity() and activity.is_cancelled():
                raise asyncio.CancelledError
            if outcome.result.status is not ExecutionStatus.SUCCEEDED:
                error = outcome.result.error
                error = await persist_failure_snapshot(request, payload, attempt_id, error,
                    operation_plan, declarations[0].sha256 if declarations else None,
                    validate_snapshot=constraint_validation.validate_snapshot)
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
                        "operation_id": error.operation_id if error else None,
                        "action": error.action if error else None,
                        "details": dict(error.details) if error else {},
                        "evidence": dict(error.evidence) if error else {},
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

            if any(op.action=='sketch.patch_relations' for op in operation_plan.operations):
                metadata_path=outcome.files.get('capability-result')
                try:
                    if metadata_path is None:
                        raise ValueError('关系编辑缺少原生验证产物')
                    reports=constraint_validation.verify_relation_receipts(json.loads(metadata_path.read_text(encoding='utf-8')),
                        operation_plan.model_dump(mode='json'))
                except (ValueError,KeyError,TypeError) as exc:
                    raise ApplicationError(str(exc),type='sketch_relationship_verification_failed',non_retryable=True) from exc
                for report in reports:
                    await persist_repair_verification(request,source_id=payload['source_id'],source_hash=source_hash,
                        attempt_id=attempt_id,report=report)
            if repair_contract:
                metadata_path = outcome.files.get('capability-result')
                if metadata_path is None:
                    raise ApplicationError('constraint repair validation evidence is missing',
                        type='constraint_repair_verification_failed', non_retryable=True)
                try:
                    native_report = constraint_validation.verify_receipt(json.loads(metadata_path.read_text(encoding='utf-8')), repair_contract)
                except (ValueError, KeyError, TypeError) as exc:
                    raise ApplicationError(str(exc), type='constraint_repair_verification_failed',
                                           non_retryable=True) from exc
                await persist_repair_verification(request, source_id=payload['source_id'], source_hash=source_hash,
                    attempt_id=attempt_id, report=native_report)
            if profile_contract:
                metadata_path = outcome.files.get('capability-result')
                if metadata_path is None:
                    raise ApplicationError('profile replan validation evidence is missing',
                        type='profile_replan_verification_failed', non_retryable=True)
                try:
                    native_report = constraint_validation.verify_profile_receipt(json.loads(metadata_path.read_text()), profile_contract)
                except (ValueError, KeyError, TypeError) as exc:
                    raise ApplicationError(str(exc), type='profile_replan_verification_failed', non_retryable=True) from exc
                await persist_profile_verification(request, source_id=payload['source_id'], source_hash=source_hash,
                    attempt_id=attempt_id, report=native_report)
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
            verification_outputs: list[dict[str, Any]] = []
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
                (verification_outputs if output_name == 'verification_step' else staged_outputs).append(
                    {
                        "format": 'step' if output_name == 'verification_step' else output_name,
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
            if verification_outputs:
                manifest['verification_outputs'] = verification_outputs
            if payload.get("checkpoint_enabled"):
                manifest["execution_mode"] = operation_plan.execution_mode
                manifest["predecessor_checkpoint_id"] = payload.get("checkpoint_manifest_id")
            result_payload = {
                "status": "succeeded",
                "attempt_id": str(attempt_id),
                "source_id": str(payload["source_id"]),
                "source_hash": source_hash,
                "outputs": staged_outputs,
                "execution_result": outcome.result.model_dump(mode="json"),
                "tool_result": execution_receipt(source_id=str(payload["source_id"]),
                    source_hash=source_hash, attempt_id=str(attempt_id), outputs=staged_outputs),
            }
            if task_inputs.get('base'):
                metadata_path = outcome.files.get('capability-result')
                baseline = ((json.loads(metadata_path.read_text(encoding='utf-8')).get('result') or {}).get('source_baseline')
                    if metadata_path else None)
                if baseline is not None:
                    if (baseline.get('sha256') != declarations[0].sha256 or type(baseline.get('solid_count')) is not int
                            or baseline['solid_count'] < 0 or native_import and baseline['solid_count'] < 1):
                        raise ApplicationError('原生输入实体数量证据无效',type='native_source_geometry_invalid',non_retryable=True)
                    result_payload['source_solid_count'] = baseline['solid_count']
                elif native_import:
                    raise ApplicationError('原生导入缺少输入几何证据',type='native_source_geometry_missing',non_retryable=True)
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
