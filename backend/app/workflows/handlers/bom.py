"""Bom use cases; Temporal names live in the adapter."""
from __future__ import annotations
from app.config import settings
import asyncio
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID
from sqlalchemy import text
from temporalio import activity
from app.workflows.execution_support import execution_identity, execution_timeout
from temporalio.exceptions import ApplicationError
from app.agent.durable_plan import AgentPlan
from app.db import tenant_transaction
from app.execution.backend import ExecutionBackend, MaterializedExecutionOutcome
from app.execution.canonical import canonical_sha256
from app.execution.contracts import ArtifactInput, ExecutionError, ExecutionSource, ExecutionSpec, ExecutionStatus, OutputDeclaration, ResourceLimits, RuntimeRequirement
from app.freecad.bom_contracts import FreeCADBOMDocumentV1, FreeCADBOMRequestV1
from app.object_store import download_object, put_file
from app.workflows.inputs import _uuid, _agent_v2_request
from app.workflows.handlers.legacy_modeling import plan
from app.workflows.execution_support import _prepare_agent_validation_attempt
from app.workflows.execution_support import _run_backend_with_heartbeats
from app.workflows.execution_support import _mark_execution_failure
from app.workflows.validation_support import _record_agent_validation_outcome

async def agent_generate_bom(payload: dict[str, Any], *, backend: ExecutionBackend) -> dict[str, Any]:
    info = execution_identity()
    request = _agent_v2_request(payload)
    plan = AgentPlan.model_validate(payload["plan"])
    candidate_build_id = _uuid(payload, "candidate_build_id")
    step_key = "agent-native-bom"
    attempt_id, step_id, lease_token, lease_generation = (
        await _prepare_agent_validation_attempt(
            payload,
            temporal_attempt=info.attempt,
            step_key=step_key,
            step_index=None if payload.get("checkpoint_enabled") else 60_000,
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
        snapshot = await asyncio.to_thread(backend.runtime_snapshot)
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
            limits=ResourceLimits.from_configured_memory(settings.sandbox_memory_limit,
                timeout_seconds=execution_timeout(payload, 180),
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
            backend,
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
        # Sandbox outputs belong to its isolated UID. Preserve the original
        # result and write the provenance envelope in our own staging dir.
        document_path = temp_dir / "bom.json"
        document_path.write_text(
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
            path = document_path if role == "bom-json" else outcome.files[role]
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
