"""Geometry validation use cases; Temporal names live in the adapter."""
from __future__ import annotations
from app.config import settings
from app.contracts.acceptance import AcceptanceContract, acceptance_outcome
from app.contracts.geometry_request import geometry_request_digest
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
from app.execution.backend import ExecutionBackend, MaterializedExecutionOutcome
from app.execution.contracts import ArtifactInput, ExecutionSource, ExecutionSpec, ExecutionStatus, OutputDeclaration, ResourceLimits, RuntimeRequirement
from app.object_store import download_object
from app.repositories.agent_candidates import get_validation_evidence_for_manifest
from app.validation.durable_geometry import DurableGeometryReport, indeterminate_geometry_report, verify_geometry_evidence
from app.workflows.inputs import _uuid, _agent_v2_request
from app.workflows.execution_support import _prepare_agent_validation_attempt
from app.workflows.execution_support import _run_backend_with_heartbeats
from app.workflows.execution_support import _mark_execution_failure
from app.workflows.validation_support import _record_agent_validation_outcome

async def agent_validate_geometry(payload: dict[str, Any], *, backend: ExecutionBackend) -> dict[str, Any]:
    info = execution_identity()
    request = _agent_v2_request(payload)
    acceptance = (AcceptanceContract.model_validate(payload["acceptance"])
                  if payload.get("acceptance") is not None else None)
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
    expected_solid_count = payload.get("expected_solid_count")
    guarded = acceptance is not None or expected_solid_count is not None
    request_digest = geometry_request_digest(
        expected_dimensions_mm=expected_dimensions, dimension_tolerance=dimension_tolerance,
        expected_solid_count=expected_solid_count,
        acceptance=acceptance.model_dump(mode="json") if acceptance else None)
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
        if replay is not None and guarded:
            try:
                cached = dict(replay["evidence"])
                cached.pop("runtime_provenance", None)
                verify_geometry_evidence(DurableGeometryReport.model_validate(cached),
                    request_sha256=request_digest, acceptance=acceptance,
                    expected_solid_count=expected_solid_count)
            except ValueError:
                replay = None
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
    # Delivery choice is not an input policy for independent BRep measurements.
    # Internal inputs are in the same immutable execution manifest, but are not
    # published as requested exports or added to the document's download list.
    if acceptance is not None and not any(item['format'] == 'step' for item in model_outputs):
        model_outputs += tuple(dict(item) for item in manifest.get('verification_outputs', ())
                               if item.get('format') == 'step')
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
            step_index=None if payload.get("checkpoint_enabled") else step_index,
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
                    **({"expected_solid_count": payload["expected_solid_count"]}
                       if "expected_solid_count" in payload else {}),
                    **({"acceptance": acceptance.model_dump(mode="json")} if acceptance else {}),
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
                limits=ResourceLimits.from_configured_memory(settings.sandbox_memory_limit, timeout_seconds=execution_timeout(payload, 120)),
                metadata={
                    "candidate_build_id": str(candidate_build_id),
                    "staging_manifest_id": str(manifest_id),
                    "gate": "geometry",
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
                        if guarded:
                            verify_geometry_evidence(report, request_sha256=request_digest,
                                acceptance=acceptance, expected_solid_count=expected_solid_count)
                            expected_artifacts = {(f"artifact-{n:02d}", i['format'], i['sha256'], i['size_bytes']) for n, i in enumerate(model_outputs)}
                            measured_artifacts = {(i.role, i.format, i.sha256, i.size_bytes) for i in report.artifacts}
                            if measured_artifacts != expected_artifacts or len(report.artifacts) != len(model_outputs):
                                raise ValueError("geometry evidence does not describe the supplied artifacts")
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
