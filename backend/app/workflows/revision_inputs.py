"""Revision inputs shared by narrow activity handlers."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID
from sqlalchemy import text
from temporalio.exceptions import ApplicationError
from app.db import tenant_transaction
from app.execution.contracts import ArtifactInput
from app.freecad.contracts import FreeCADOperationPlan
from app.object_store import download_object, get_object
from app.repositories.artifacts import committed_artifact_for_revision
from app.workflows.temporal import McadAgentWorkflowV2Request

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


async def _freecad_revision_artifact(request: McadAgentWorkflowV2Request, artifact_kind: str) -> dict[str, Any]:
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


async def _freecad_revision_state(request: McadAgentWorkflowV2Request) -> dict[str, Any]:
    artifact = await _freecad_revision_artifact(request, "state")
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
        selection = await conn.scalar(text("SELECT arguments->'_selection_context' FROM cad_operations WHERE id=:id"),
                                      {"id": request.workflow_run_id})
        if request.operation_context and request.operation_context.selection_context:
            if not selection or selection.get("parameter_state_sha256") != str(artifact["sha256"]):
                raise ValueError("冻结的选择与原生检查点不一致")
            state = {**state, "selection_context": selection}
        if references:
            from app.services.engineering_evidence import verified_engineering_context
            state = {**state, 'engineering_evidence': await verified_engineering_context(conn, references,
                request.branch_id, request.expected_base_revision_id)}
    if annotations:
        state = {**state, "feature_annotations": annotations}
    from app.freecad.reference_geometry import normalize_reference_state
    return normalize_reference_state(state)


async def _agent_validation_input(*, request: McadAgentWorkflowV2Request, candidate_build_id: UUID, manifest_id: UUID, temp_dir: Path, preferred_format: str='stl') -> tuple[dict[str, Any], ArtifactInput, dict[str, Path]]:
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
