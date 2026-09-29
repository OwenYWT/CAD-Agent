"""Dfm validation use cases; Temporal names live in the adapter."""
from __future__ import annotations
from app.config import settings
import asyncio
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any
from temporalio import activity
from app.workflows.execution_support import execution_identity, execution_timeout
from app.db import tenant_transaction
from app.execution.backend import ExecutionBackend, MaterializedExecutionOutcome
from app.execution.contracts import ArtifactInput, ExecutionSource, ExecutionSpec, ExecutionStatus, OutputDeclaration, ResourceLimits, RuntimeRequirement
from app.object_store import put_file
from app.repositories.agent_candidates import get_validation_evidence_for_manifest
from app.validation.durable_dfm import DurableDFMReport, indeterminate_dfm_report
from app.validation.dfm_policy_snapshot import resolve_dfm_policy_snapshot
from app.workflows.inputs import _uuid, _agent_v2_request
from app.workflows.execution_support import _prepare_agent_validation_attempt
from app.workflows.execution_support import _run_backend_with_heartbeats
from app.workflows.execution_support import _mark_execution_failure
from app.workflows.validation_support import _record_agent_validation_outcome
from app.workflows.revision_inputs import _agent_validation_input

async def agent_validate_dfm(payload: dict[str, Any], *, backend: ExecutionBackend) -> dict[str, Any]:
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
            step_index=None if payload.get("checkpoint_enabled") else int(payload["step_index"]),
            step_kind="agent_dfm_validation",
        )
    )
    temp_dir = Path(tempfile.mkdtemp(prefix="agent_dfm_"))
    outcome: MaterializedExecutionOutcome | None = None
    try:
        _, declaration, materialized = await _agent_validation_input(
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
        snapshot = await asyncio.to_thread(backend.runtime_snapshot)
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
            limits=ResourceLimits.from_configured_memory(settings.sandbox_memory_limit, timeout_seconds=execution_timeout(payload, 120)),
            metadata={
                "candidate_build_id": str(candidate_build_id),
                "staging_manifest_id": str(manifest_id),
                "gate": "dfm",
                "policy_hash": policy.policy_hash,
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
