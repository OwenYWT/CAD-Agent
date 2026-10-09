"""Engineering use cases; Temporal names live in the adapter."""
from __future__ import annotations
from app.config import settings
from functools import partial
from app.workflows.handlers.legacy_modeling import execute
import asyncio
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from sqlalchemy import text
from temporalio import activity
from temporalio.exceptions import ApplicationError
from app.db import tenant_transaction
from app.dfm.models import StepAnalysisResult
from app.dfm.step_analyzer_script import STEP_ANALYSIS_SCRIPT
from app.domain.runs import WorkflowStatus
from app.execution.backend import ExecutionBackend, MaterializedExecutionOutcome
from app.execution.contracts import ArtifactInput, ExecutionSource, ExecutionSpec, ExecutionStatus, OutputDeclaration, ResourceLimits, RuntimeRequirement
from app.object_store import get_object, put_file
from app.services.artifact_commit import authorize_artifact_upload, commit_artifacts
from app.services.design_analysis import build_design_analysis_response
from app.services.run_state import transition_workflow
from app.validation.dfm_analyzer import DFMAnalyzer
from app.workflows.logical_steps import _workflow_status
from app.workflows.inputs import _uuid
from app.workflows.execution_support import _prepare_execution_attempt
from app.workflows.execution_support import _heartbeat_loop
from app.workflows.execution_support import _run_backend_with_heartbeats
from app.workflows.execution_support import _mark_execution_failure
from app.workflows.validation_support import _complete_check_workflow

async def engineering_compute(payload: dict[str, Any], *, backend: ExecutionBackend) -> dict[str, Any]:
    from app.workflows.engineering_activities import compute_engineering
    return await compute_engineering(payload, execute=partial(execute, backend=backend))


async def scene_compute(payload: dict[str, Any], *, backend: ExecutionBackend) -> dict[str, Any]:
    from app.workflows.scene_activities import compute_scene
    return await compute_scene(payload, backend=backend, execute=partial(execute, backend=backend))


async def check(payload: dict[str, Any], *, backend: ExecutionBackend) -> dict[str, Any]:
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
        if (report_document.get("source_revision_id") != str(source_revision_id)
                or report_document.get("workflow_run_id") != str(workflow_id)):
            raise ValueError("Persisted engineering report source mismatch")
        if payload.get("rule_configuration") is not None:
            from app.services.engineering_checks import execution_rule_configuration
            async with tenant_transaction(tenant_id, principal_id) as connection:
                configuration = await execution_rule_configuration(connection,
                    tenant_id=tenant_id, principal_id=principal_id, workflow_id=workflow_id,
                    process=payload.get("process"), supplied=payload["rule_configuration"])
            if (report_document.get("rule_configuration") != configuration
                    or analysis.get("rule_configuration") != configuration):
                raise ValueError("Persisted engineering report configuration mismatch")
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
            backend.runtime_snapshot
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
            limits=ResourceLimits.from_configured_memory(settings.sandbox_memory_limit,
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
            backend,
            spec,
            tenant_id=tenant_id,
            principal_id=principal_id,
            attempt_id=attempt_id,
            lease_token=lease_token,
            lease_generation=lease_generation,
            materialized_inputs=materialized,
        )

        if activity.in_activity() and activity.is_cancelled():
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
        from app.services.engineering_checks import execution_rule_configuration
        async with tenant_transaction(tenant_id, principal_id) as connection:
            rule_configuration = await execution_rule_configuration(connection,
                tenant_id=tenant_id, principal_id=principal_id, workflow_id=workflow_id,
                process=payload.get("process"), supplied=payload.get("rule_configuration"))
        design_analysis = await analyzer.analyze(
            stl_path=source_artifacts["stl"]["local_path"],
            code=str(payload.get("code") or ""),
            description=str(payload.get("description") or ""),
            process=payload.get("process"),
            material=payload.get("material"),
            precomputed_step_data=step_data,
            rule_configuration=rule_configuration,
        )
        response = build_design_analysis_response(design_analysis)
        analysis = response.model_dump(mode="json")
        analysis.update(source_revision_id=str(source_revision_id), process=payload.get("process"), material=payload.get("material"))
        analysis["rule_configuration"] = rule_configuration
        report_document = {
            "schema_version": "dfm-report.v1",
            "workflow_run_id": str(workflow_id),
            "source_workflow_run_id": str(source_workflow_id),
            "source_revision_id": str(source_revision_id),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "step_analysis_error": step_analysis_error,
            "rule_configuration": rule_configuration,
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
