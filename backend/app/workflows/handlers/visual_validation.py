"""Visual validation use cases; Temporal names live in the adapter."""
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
from app.db import tenant_transaction
from app.domain.runs import StepStatus
from app.execution.backend import ExecutionBackend, MaterializedExecutionOutcome
from app.execution.contracts import ExecutionSource, ExecutionSpec, ExecutionStatus, OutputDeclaration, ResourceLimits, RuntimeRequirement
from app.object_store import download_object, put_file
from app.repositories.agent_candidates import get_generated_source_for_step, get_validation_evidence_for_manifest, record_generated_source, record_validation_evidence
from app.repositories.runs import append_workflow_event
from app.services.run_state import complete_attempt, transition_step
from app.validation.durable_visual import DurableVisualValidator, VisualRenderEvidence, indeterminate_visual_report, brief_with_verified_geometry
from app.workflows.logical_steps import _start_agent_logical_step, _fail_agent_logical_step
from app.workflows.inputs import _uuid, _agent_v2_request
from app.workflows.execution_support import _prepare_agent_validation_attempt
from app.workflows.execution_support import _stored_validation_attempt_result
from app.workflows.execution_support import _run_backend_with_heartbeats
from app.workflows.execution_support import _mark_execution_failure
from app.workflows.validation_support import _record_agent_validation_outcome
from app.workflows.revision_inputs import _agent_validation_input

async def agent_render_visual(payload: dict[str, Any], *, backend: ExecutionBackend) -> dict[str, Any]:
    info = execution_identity()
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
            step_index=None if payload.get("checkpoint_enabled") else int(payload["step_index"]),
            step_kind="agent_visual_render",
        )
    )
    temp_dir = Path(tempfile.mkdtemp(prefix="agent_visual_"))
    outcome: MaterializedExecutionOutcome | None = None
    try:
        try:
            _, declaration, materialized = await _agent_validation_input(
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
        snapshot = await asyncio.to_thread(backend.runtime_snapshot)
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
            limits=ResourceLimits.from_configured_memory(settings.sandbox_memory_limit,
                timeout_seconds=execution_timeout(payload, 120),
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
            backend,
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


async def agent_judge_visual(payload: dict[str, Any], *, durable_visual: DurableVisualValidator) -> dict[str, Any]:
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
        geometry_record = await get_validation_evidence_for_manifest(
            connection,tenant_id=request.tenant_id,candidate_build_id=candidate_build_id,
            workflow_id=request.workflow_run_id,staging_manifest_id=manifest_id,gate='geometry')
        visual_brief = brief_with_verified_geometry(dict(payload['design_brief']),geometry_record,manifest_id)
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
        report = await durable_visual.report(
            objective=str(payload["objective"]),
            design_brief=visual_brief,
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


async def agent_repair_visual(payload: dict[str, Any], *, durable_visual: DurableVisualValidator) -> dict[str, Any]:
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
            step_index=None if payload.get("checkpoint_enabled") else int(payload["step_index"]),
            kind="agent_visual_repair",
        )
    try:
        repaired, provenance = await durable_visual.repair(
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
