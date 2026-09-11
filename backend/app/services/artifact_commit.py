"""Two-phase S3 artifact authorization, verification, and atomic DB commit."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from botocore.exceptions import ClientError
from sqlalchemy import text

from app.config import settings
from app.db import tenant_transaction
from app.domain.artifacts import (
    ArtifactCommitResult,
    ArtifactUploadAuthorization,
    CandidateSealResult,
    CommittedArtifact,
)
from app.domain.revisions import CandidateBuildStatus
from app.domain.runs import AttemptStatus, WorkflowStatus
from app.execution.canonical import canonical_sha256
from app.object_store import (
    copy_object,
    create_presigned_upload,
    delete_object,
    head_object,
    list_objects,
    sha256_object,
)
from app.repositories.artifacts import (
    artifacts_for_uploads,
    insert_artifact,
    insert_upload_authorization,
    locked_uploads,
    mark_uploads_committed,
    reject_uploads,
)
from app.repositories.runs import append_workflow_event
from app.repositories.agent_candidates import (
    CandidateBuildConflict,
    create_candidate_seal,
    transition_agent_candidate_build,
)
from app.repositories.revisions import create_candidate_change_set
from app.services.change_sets import build_agent_change_set_evidence
from app.services.run_state import (
    IdempotencyConflict,
    IllegalTransition,
    StaleLease,
    complete_attempt,
    transition_workflow,
)


class ArtifactVerificationError(RuntimeError):
    """Uploaded bytes do not match their immutable authorization."""


class CandidateSealVerificationError(RuntimeError):
    """Selected candidate staging bytes or evidence cannot be sealed."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _candidate_final_object_key(
    *,
    tenant_id: UUID,
    project_id: UUID,
    candidate_build_id: UUID,
    sha256: str,
    filename: str,
) -> str:
    return (
        f"tenants/{tenant_id}/projects/{project_id}/candidate-seals/"
        f"{candidate_build_id}/artifacts/{sha256}/{filename}"
    )


async def _verified_copy_if_absent(
    *,
    source_key: str,
    destination_key: str,
    expected_sha256: str,
    expected_size: int,
) -> None:
    destination_exists = False
    try:
        destination = await sha256_object(destination_key)
        destination_exists = True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in {"404", "NoSuchKey", "NotFound"}:
            raise
    if destination_exists:
        if (
            destination["sha256"] != expected_sha256
            or destination["size_bytes"] != expected_size
        ):
            raise CandidateSealVerificationError(
                "existing final object does not match selected manifest"
            )
        return
    try:
        source = await sha256_object(source_key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            raise CandidateSealVerificationError(
                "selected staging object is missing"
            ) from exc
        raise
    if source["sha256"] != expected_sha256 or source["size_bytes"] != expected_size:
        raise CandidateSealVerificationError(
            "selected staging object failed SHA-256 verification"
        )
    await copy_object(source_key, destination_key)
    copied = await sha256_object(destination_key)
    if copied["sha256"] != expected_sha256 or copied["size_bytes"] != expected_size:
        raise CandidateSealVerificationError(
            "final object failed post-copy SHA-256 verification"
        )


def _candidate_artifact_rows(
    *,
    tenant_id: UUID,
    project_id: UUID,
    candidate_build_id: UUID,
    manifests: list[dict[str, Any]],
    evidence_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    multiple = len(manifests) > 1
    rows: list[dict[str, Any]] = []
    filenames: set[str] = set()
    for selected in manifests:
        step_key = str(selected["step_key"])
        manifest = dict(selected["manifest"])
        outputs = list(manifest.get("outputs") or ())
        if not outputs:
            raise CandidateSealVerificationError(
                f"selected manifest {step_key} has no outputs"
            )
        for output in outputs:
            filename = _safe_filename(str(output["filename"]))
            product_filename = _safe_filename(
                f"{step_key}-{filename}" if multiple else filename
            )
            if product_filename in filenames:
                raise CandidateSealVerificationError(
                    "selected artifact filenames are not unique"
                )
            filenames.add(product_filename)
            digest = str(output["sha256"])
            size_bytes = int(output["size_bytes"])
            if not _SHA256_RE.fullmatch(digest) or size_bytes < 1:
                raise CandidateSealVerificationError(
                    "selected artifact declaration is invalid"
                )
            source_key = str(output["object_key"])
            expected_prefix = (
                f"staging/agent/tenants/{tenant_id}/candidates/"
                f"{candidate_build_id}/"
            )
            if not source_key.startswith(expected_prefix):
                raise CandidateSealVerificationError(
                    "selected artifact key is outside its candidate staging prefix"
                )
            rows.append(
                {
                    "manifest_id": selected["id"],
                    "attempt_id": selected["execution_attempt_id"],
                    "step_key": step_key,
                    "artifact_kind": str(output["format"]).lower(),
                    "filename": product_filename,
                    "content_type": str(output["content_type"]),
                    "size_bytes": size_bytes,
                    "sha256": digest,
                    "staging_object_key": source_key,
                    "object_key": _candidate_final_object_key(
                        tenant_id=tenant_id,
                        project_id=project_id,
                        candidate_build_id=candidate_build_id,
                        sha256=digest,
                        filename=product_filename,
                    ),
                    "runtime_metadata": dict(
                        manifest.get("runtime_provenance") or {}
                    ),
                }
            )
    manifest_steps = {row["id"]: row["step_key"] for row in manifests}
    for evidence in evidence_rows:
        step_key = manifest_steps[evidence["staging_manifest_id"]]
        report = dict(evidence["evidence"] or {})
        if evidence["gate"] == "visual":
            for render in report.get("renders") or ():
                view = str(render["view"])
                product_filename = _safe_filename(
                    f"{step_key}-visual-{view}.png"
                )
                if product_filename in filenames:
                    raise CandidateSealVerificationError(
                        "selected evidence filenames are not unique"
                    )
                filenames.add(product_filename)
                digest = str(render["sha256"])
                size_bytes = int(render["size_bytes"])
                source_key = str(render["object_key"])
                expected_prefix = (
                    f"staging/agent/tenants/{tenant_id}/candidates/"
                    f"{candidate_build_id}/validation/"
                )
                if (
                    not _SHA256_RE.fullmatch(digest)
                    or size_bytes < 1
                    or not source_key.startswith(expected_prefix)
                    or evidence["execution_attempt_id"] is None
                ):
                    raise CandidateSealVerificationError(
                        "selected visual evidence declaration is invalid"
                    )
                rows.append(
                    {
                        "manifest_id": evidence["staging_manifest_id"],
                        "attempt_id": evidence["execution_attempt_id"],
                        "step_key": step_key,
                        "artifact_kind": "visual_render",
                        "filename": product_filename,
                        "content_type": "image/png",
                        "size_bytes": size_bytes,
                        "sha256": digest,
                        "staging_object_key": source_key,
                        "object_key": _candidate_final_object_key(
                            tenant_id=tenant_id,
                            project_id=project_id,
                            candidate_build_id=candidate_build_id,
                            sha256=digest,
                            filename=product_filename,
                        ),
                        "runtime_metadata": {
                            **dict(report.get("runtime_provenance") or {}),
                            "validation_evidence_id": str(evidence["id"]),
                            "validation_gate": "visual",
                            "view": view,
                        },
                    }
                )
            continue
        if evidence["gate"] == "bom":
            artifacts = list(report.get("artifacts") or ())
            if {str(item.get("role")) for item in artifacts} != {
                "bom-json",
                "bom-csv",
            }:
                raise CandidateSealVerificationError(
                    "selected BOM evidence is incomplete"
                )
            for item in artifacts:
                role = str(item["role"])
                filename = _safe_filename(str(item["filename"]))
                product_filename = _safe_filename(f"{step_key}-{filename}")
                digest = str(item.get("sha256") or "")
                size_bytes = int(item.get("size_bytes") or 0)
                source_key = str(item.get("object_key") or "")
                expected_prefix = (
                    f"staging/agent/tenants/{tenant_id}/candidates/"
                    f"{candidate_build_id}/validation/"
                )
                if (
                    product_filename in filenames
                    or not _SHA256_RE.fullmatch(digest)
                    or size_bytes < 1
                    or not source_key.startswith(expected_prefix)
                    or evidence["execution_attempt_id"] is None
                ):
                    raise CandidateSealVerificationError(
                        "selected BOM artifact declaration is invalid"
                    )
                filenames.add(product_filename)
                rows.append({
                    "manifest_id": evidence["staging_manifest_id"],
                    "attempt_id": evidence["execution_attempt_id"],
                    "step_key": step_key,
                    "artifact_kind": (
                        "bom_json" if role == "bom-json" else "bom_csv"
                    ),
                    "filename": product_filename,
                    "content_type": str(item["content_type"]),
                    "size_bytes": size_bytes,
                    "sha256": digest,
                    "staging_object_key": source_key,
                    "object_key": _candidate_final_object_key(
                        tenant_id=tenant_id,
                        project_id=project_id,
                        candidate_build_id=candidate_build_id,
                        sha256=digest,
                        filename=product_filename,
                    ),
                    "runtime_metadata": {
                        **dict(report.get("runtime_provenance") or {}),
                        "validation_evidence_id": str(evidence["id"]),
                        "validation_gate": "bom",
                    },
                })
            continue
        if evidence["gate"] != "dfm":
            continue
        report_artifact = dict(report.get("report_artifact") or {})
        if not report_artifact:
            continue
        step_key = manifest_steps[evidence["staging_manifest_id"]]
        product_filename = _safe_filename(f"{step_key}-dfm-report.json")
        digest = str(report_artifact.get("sha256") or "")
        size_bytes = int(report_artifact.get("size_bytes") or 0)
        source_key = str(report_artifact.get("object_key") or "")
        expected_prefix = (
            f"staging/agent/tenants/{tenant_id}/candidates/"
            f"{candidate_build_id}/validation/"
        )
        if (
            product_filename in filenames
            or not _SHA256_RE.fullmatch(digest)
            or size_bytes < 1
            or not source_key.startswith(expected_prefix)
            or evidence["execution_attempt_id"] is None
        ):
            raise CandidateSealVerificationError(
                "selected DFM report declaration is invalid"
            )
        filenames.add(product_filename)
        rows.append(
            {
                "manifest_id": evidence["staging_manifest_id"],
                "attempt_id": evidence["execution_attempt_id"],
                "step_key": step_key,
                "artifact_kind": "dfm_report",
                "filename": product_filename,
                "content_type": "application/json",
                "size_bytes": size_bytes,
                "sha256": digest,
                "staging_object_key": source_key,
                "object_key": _candidate_final_object_key(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    candidate_build_id=candidate_build_id,
                    sha256=digest,
                    filename=product_filename,
                ),
                "runtime_metadata": {
                    **dict(report.get("runtime_provenance") or {}),
                    "validation_evidence_id": str(evidence["id"]),
                    "validation_gate": "dfm",
                },
            }
        )
    return rows


async def _load_candidate_seal_selection(
    connection,
    *,
    tenant_id: UUID,
    candidate_build_id: UUID,
    workflow_id: UUID,
    plan: dict[str, Any],
    selected: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    candidate = (
        await connection.execute(
            text(
                """
                SELECT * FROM agent_candidate_builds
                WHERE tenant_id=:tenant_id AND id=:candidate_build_id
                  AND workflow_run_id=:workflow_id
                FOR UPDATE
                """
            ),
            {
                "tenant_id": tenant_id,
                "candidate_build_id": candidate_build_id,
                "workflow_id": workflow_id,
            },
        )
    ).mappings().one_or_none()
    if candidate is None:
        raise CandidateSealVerificationError("candidate build ownership mismatch")
    if candidate["status"] != CandidateBuildStatus.BUILDING.value:
        raise CandidateBuildConflict(
            f"candidate cannot seal while it is {candidate['status']}"
        )
    if candidate["plan_hash"] != canonical_sha256(plan):
        raise CandidateSealVerificationError("candidate plan hash mismatch")
    expected_steps = [
        str(step["step_key"])
        for step in plan.get("steps") or ()
        if step.get("output_formats")
    ]
    supplied_steps = [str(item["step_key"]) for item in selected]
    if not expected_steps or supplied_steps != expected_steps:
        raise CandidateSealVerificationError(
            "selected manifests do not match terminal plan outputs"
        )
    manifest_ids = [UUID(str(item["staging_manifest_id"])) for item in selected]
    if len(manifest_ids) != len(set(manifest_ids)):
        raise CandidateSealVerificationError("selected manifests contain duplicates")
    rows = (
        await connection.execute(
            text(
                """
                SELECT m.*, a.status AS attempt_status
                FROM agent_staging_manifests m
                JOIN execution_attempts a ON a.id=m.execution_attempt_id
                WHERE m.tenant_id=:tenant_id AND m.id = ANY(:manifest_ids)
                """
            ),
            {"tenant_id": tenant_id, "manifest_ids": manifest_ids},
        )
    ).mappings().all()
    by_id = {row["id"]: dict(row) for row in rows}
    if set(by_id) != set(manifest_ids):
        raise CandidateSealVerificationError("selected manifest is missing")
    manifests: list[dict[str, Any]] = []
    for supplied, manifest_id in zip(selected, manifest_ids, strict=True):
        row = by_id[manifest_id]
        manifest = dict(row["manifest"])
        source = (
            await connection.execute(
                text(
                    """
                    SELECT source_hash, candidate_build_id, workflow_run_id
                    FROM agent_generated_sources
                    WHERE tenant_id=:tenant_id AND id=:source_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "source_id": UUID(str(manifest.get("source_id"))),
                },
            )
        ).mappings().one_or_none()
        is_superseded = await connection.scalar(
            text(
                "SELECT 1 FROM agent_staging_manifests "
                "WHERE tenant_id=:tenant_id AND supersedes_id=:manifest_id LIMIT 1"
            ),
            {"tenant_id": tenant_id, "manifest_id": manifest_id},
        )
        consumed = await connection.scalar(
            text(
                "SELECT 1 FROM agent_seal_manifests "
                "WHERE tenant_id=:tenant_id AND staging_manifest_id=:manifest_id"
            ),
            {"tenant_id": tenant_id, "manifest_id": manifest_id},
        )
        if (
            row["candidate_build_id"] != candidate_build_id
            or row["workflow_run_id"] != workflow_id
            or row["status"] != "accepted"
            or row["attempt_status"] != "succeeded"
            or is_superseded is not None
            or consumed is not None
            or source is None
            or source["candidate_build_id"] != candidate_build_id
            or source["workflow_run_id"] != workflow_id
            or source["source_hash"] != manifest.get("source_hash")
            or manifest.get("plan_step_key") != supplied["step_key"]
            or row["manifest_hash"] != supplied["manifest_hash"]
            or str(manifest.get("source_id")) != str(supplied["source_id"])
            or manifest.get("source_hash") != supplied["source_hash"]
        ):
            raise CandidateSealVerificationError(
                "selected manifest is stale, consumed, or does not match selection"
            )
        manifests.append({**row, "step_key": supplied["step_key"]})
    policy = dict(plan["validation_policy"])
    evidence_rows: list[dict[str, Any]] = []
    for supplied, manifest_id in zip(selected, manifest_ids, strict=True):
        for gate in ("geometry", "visual", "dfm", "bom"):
            mode = str(policy[gate]["mode"])
            evidence_id = supplied.get(f"{gate}_evidence_id")
            if (
                gate == "bom"
                and plan["model_kind"] == "assembly"
                and supplied["step_key"] != next(
                step["step_key"]
                for step in plan["steps"]
                if step["kind"] == "assembly_combine"
                )
            ):
                if evidence_id is not None:
                    raise CandidateSealVerificationError(
                        "BOM evidence belongs only to the assembly combine manifest"
                    )
                continue
            if mode == "disabled":
                if evidence_id is not None:
                    raise CandidateSealVerificationError(
                        f"disabled {gate} gate cannot select evidence"
                    )
                continue
            if evidence_id is None:
                raise CandidateSealVerificationError(
                    f"selected manifest is missing {gate} evidence"
                )
            evidence = (
                await connection.execute(
                    text(
                        """
                        SELECT * FROM agent_validation_evidence
                        WHERE tenant_id=:tenant_id AND id=:evidence_id
                        """
                    ),
                    {"tenant_id": tenant_id, "evidence_id": UUID(str(evidence_id))},
                )
            ).mappings().one_or_none()
            if (
                evidence is None
                or evidence["candidate_build_id"] != candidate_build_id
                or evidence["workflow_run_id"] != workflow_id
                or evidence["staging_manifest_id"] != manifest_id
                or evidence["gate"] != gate
                or evidence["mode"] != mode
                or (mode == "required" and evidence["outcome"] != "passed")
            ):
                raise CandidateSealVerificationError(
                    f"selected {gate} evidence does not satisfy its gate"
                )
            evidence_rows.append(dict(evidence))
    return dict(candidate), manifests, evidence_rows


def _seal_result_from_payload(payload: dict[str, Any], *, replayed: bool) -> CandidateSealResult:
    return CandidateSealResult(
        seal_id=UUID(str(payload["seal_id"])),
        candidate_build_id=UUID(str(payload["candidate_build_id"])),
        candidate_revision_id=UUID(str(payload["candidate_revision_id"])),
        change_set_id=UUID(str(payload["change_set_id"])),
        artifacts=tuple(
            CommittedArtifact(
                artifact_id=UUID(str(item["artifact_id"])),
                upload_id=UUID(str(item["upload_id"])),
                filename=str(item["filename"]),
                artifact_kind=str(item["artifact_kind"]),
                object_key=str(item["object_key"]),
                size_bytes=int(item["size_bytes"]),
                sha256=str(item["sha256"]),
                content_type=str(item["content_type"]),
            )
            for item in payload.get("artifacts") or ()
        ),
        replayed=replayed,
    )


async def seal_agent_candidate(
    *,
    tenant_id: UUID,
    principal_id: UUID,
    workflow_id: UUID,
    candidate_build_id: UUID,
    plan: dict[str, Any],
    selected_manifests: list[dict[str, Any]],
    fault_hook: Callable[[str], None] | None = None,
) -> CandidateSealResult:
    """Promote validated staging outputs and atomically make a candidate reviewable."""
    seal_key = f"agent-v2:{workflow_id}:candidate-seal"
    selection = {
        "schema_version": "agent-candidate-selection.v1",
        "candidate_build_id": str(candidate_build_id),
        "workflow_run_id": str(workflow_id),
        "manifests": selected_manifests,
    }

    async with tenant_transaction(tenant_id, principal_id) as connection:
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": str(candidate_build_id)},
        )
        existing = (
            await connection.execute(
                text(
                    """
                    SELECT status, result, selection_hash
                    FROM agent_candidate_seals
                    WHERE tenant_id=:tenant_id
                      AND candidate_build_id=:candidate_build_id
                      AND seal_key=:seal_key
                    FOR UPDATE
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "candidate_build_id": candidate_build_id,
                    "seal_key": seal_key,
                },
            )
        ).mappings().one_or_none()
        if existing is not None and existing["selection_hash"] != canonical_sha256(
            selection
        ):
            raise CandidateBuildConflict(
                "candidate seal replay used a different selection"
            )
        if (
            existing is not None
            and existing["status"] == "committed"
            and existing["result"] is not None
        ):
            replay = _seal_result_from_payload(
                dict(existing["result"]), replayed=True
            )
            staging_keys = list(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT staging_object_key FROM artifact_uploads
                            WHERE tenant_id=:tenant_id
                              AND revision_id=:revision_id
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "revision_id": replay.candidate_revision_id,
                        },
                    )
                ).scalars()
            )
            for key in staging_keys:
                try:
                    await delete_object(key)
                except Exception:
                    pass
            return replay
        candidate, manifests, evidence = await _load_candidate_seal_selection(
            connection,
            tenant_id=tenant_id,
            candidate_build_id=candidate_build_id,
            workflow_id=workflow_id,
            plan=plan,
            selected=selected_manifests,
        )
        seal = await create_candidate_seal(
            connection,
            tenant_id=tenant_id,
            candidate_build_id=candidate_build_id,
            seal_key=seal_key,
            selection=selection,
        )
        await connection.execute(
            text(
                """
                UPDATE agent_candidate_seals
                SET status='copying', error_code=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=:tenant_id AND id=:seal_id
                  AND status IN ('pending', 'copying', 'failed')
                """
            ),
            {"tenant_id": tenant_id, "seal_id": seal.seal_id},
        )
    artifact_rows = _candidate_artifact_rows(
        tenant_id=tenant_id,
        project_id=candidate["project_id"],
        candidate_build_id=candidate_build_id,
        manifests=manifests,
        evidence_rows=evidence,
    )
    try:
        for index, row in enumerate(artifact_rows):
            await _verified_copy_if_absent(
                source_key=row["staging_object_key"],
                destination_key=row["object_key"],
                expected_sha256=row["sha256"],
                expected_size=row["size_bytes"],
            )
            if index == 0 and fault_hook is not None:
                fault_hook("after_partial_copy")
        if fault_hook is not None:
            fault_hook("after_all_copies")
    except Exception as exc:
        async with tenant_transaction(tenant_id, principal_id) as connection:
            await connection.execute(
                text(
                    """
                    UPDATE agent_candidate_seals
                    SET status='failed', error_code=:error_code,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=:tenant_id AND id=:seal_id
                      AND status <> 'committed'
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "seal_id": seal.seal_id,
                    "error_code": type(exc).__name__[:200],
                },
            )
        raise

    validation_summary, risk_summary = build_agent_change_set_evidence(evidence)
    candidate_manifest = {
        "schema_version": "mcad-agent-revision-manifest.v1",
        "candidate_build_id": str(candidate_build_id),
        "workflow_run_id": str(workflow_id),
        "objective": str(plan["objective"]),
        "operation": str(plan["operation"]),
        "base_revision_id": str(candidate["base_revision_id"]),
        "plan_hash": candidate["plan_hash"],
        "selected_manifests": [
            {
                "staging_manifest_id": str(row["id"]),
                "manifest_hash": row["manifest_hash"],
                "plan_step_key": row["step_key"],
            }
            for row in manifests
        ],
        "artifacts": [
            {
                "filename": row["filename"],
                "artifact_kind": row["artifact_kind"],
                "object_key": row["object_key"],
                "sha256": row["sha256"],
                "size_bytes": row["size_bytes"],
                "content_type": row["content_type"],
            }
            for row in artifact_rows
        ],
        "validation": validation_summary,
        "risks": risk_summary,
        "bom": {
            "status": (
                "succeeded" if plan.get("model_kind") == "assembly"
                else "not_applicable"
            ),
            "evidence_id": next(
                (
                    str(row["id"])
                    for row in evidence
                    if row["gate"] == "bom"
                ),
                None,
            ),
            "json_artifact_kind": (
                "bom_json" if plan.get("model_kind") == "assembly" else None
            ),
            "csv_artifact_kind": (
                "bom_csv" if plan.get("model_kind") == "assembly" else None
            ),
        },
    }
    committed: list[CommittedArtifact] = []
    async with tenant_transaction(tenant_id, principal_id) as connection:
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": str(candidate_build_id)},
        )
        replay = (
            await connection.execute(
                text(
                    "SELECT status, result FROM agent_candidate_seals "
                    "WHERE tenant_id=:tenant_id AND id=:seal_id FOR UPDATE"
                ),
                {"tenant_id": tenant_id, "seal_id": seal.seal_id},
            )
        ).mappings().one()
        if replay["status"] == "committed":
            return _seal_result_from_payload(dict(replay["result"]), replayed=True)
        candidate, manifests, evidence = await _load_candidate_seal_selection(
            connection,
            tenant_id=tenant_id,
            candidate_build_id=candidate_build_id,
            workflow_id=workflow_id,
            plan=plan,
            selected=selected_manifests,
        )
        change = await create_candidate_change_set(
            connection,
            tenant_id=tenant_id,
            project_id=candidate["project_id"],
            branch_id=candidate["branch_id"],
            expected_base_revision_id=candidate["base_revision_id"],
            created_by_principal_id=principal_id,
            idempotency_key=f"agent-v2:{candidate_build_id}:change-set",
            objective=str(plan["objective"]),
            candidate_manifest=candidate_manifest,
            change_summary={
                "operation": str(plan["operation"]),
                "selected_step_count": len(manifests),
                "artifact_count": len(artifact_rows),
                "source_hashes": [
                    str(item["source_hash"]) for item in selected_manifests
                ],
            },
            validation_summary=validation_summary,
            risk_summary=risk_summary,
            source_workflow_run_id=workflow_id,
        )
        now = _utcnow()
        for row in artifact_rows:
            upload_id = uuid5(
                NAMESPACE_URL,
                f"cad-agent:{candidate_build_id}:upload:{row['manifest_id']}:"
                f"{row['filename']}:{row['sha256']}",
            )
            artifact_id = uuid5(
                NAMESPACE_URL,
                f"cad-agent:{candidate_build_id}:artifact:{row['manifest_id']}:"
                f"{row['filename']}:{row['sha256']}",
            )
            upload = {
                "id": upload_id,
                "tenant_id": tenant_id,
                "project_id": candidate["project_id"],
                "revision_id": change.candidate_revision_id,
                "workflow_run_id": workflow_id,
                "attempt_id": row["attempt_id"],
                "artifact_kind": row["artifact_kind"],
                "filename": row["filename"],
                "content_type": row["content_type"],
                "declared_size_bytes": row["size_bytes"],
                "declared_sha256": row["sha256"],
                "staging_object_key": row["staging_object_key"],
            }
            await insert_upload_authorization(
                connection,
                upload_id=upload_id,
                tenant_id=tenant_id,
                project_id=candidate["project_id"],
                revision_id=change.candidate_revision_id,
                workflow_id=workflow_id,
                attempt_id=row["attempt_id"],
                artifact_kind=row["artifact_kind"],
                filename=row["filename"],
                content_type=row["content_type"],
                declared_size_bytes=row["size_bytes"],
                declared_sha256=row["sha256"],
                staging_object_key=row["staging_object_key"],
                expires_at=now,
            )
            committed.append(
                await insert_artifact(
                    connection,
                    artifact_id=artifact_id,
                    upload=upload,
                    object_key=row["object_key"],
                    runtime_metadata={
                        **row["runtime_metadata"],
                        "candidate_build_id": str(candidate_build_id),
                        "seal_id": str(seal.seal_id),
                        "staging_manifest_id": str(row["manifest_id"]),
                    },
                )
            )
            await mark_uploads_committed(
                connection,
                upload_ids=[upload_id],
                committed_at=now,
            )
        for ordinal, manifest in enumerate(manifests):
            await connection.execute(
                text(
                    """
                    INSERT INTO agent_seal_manifests (
                        tenant_id, seal_id, staging_manifest_id, ordinal,
                        plan_step_key
                    ) VALUES (
                        :tenant_id, :seal_id, :manifest_id, :ordinal, :step_key
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "seal_id": seal.seal_id,
                    "manifest_id": manifest["id"],
                    "ordinal": ordinal,
                    "step_key": manifest["step_key"],
                },
            )
        for row in evidence:
            await connection.execute(
                text(
                    """
                    INSERT INTO agent_seal_evidence (
                        tenant_id, seal_id, evidence_id, gate
                    ) VALUES (
                        :tenant_id, :seal_id, :evidence_id, :gate
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "seal_id": seal.seal_id,
                    "evidence_id": row["id"],
                    "gate": row["gate"],
                },
            )
        await connection.execute(
            text(
                """
                UPDATE agent_candidate_builds
                SET candidate_revision_id=:revision_id,
                    change_set_id=:change_set_id,
                    updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=:tenant_id AND id=:candidate_build_id
                  AND status='building'
                """
            ),
            {
                "tenant_id": tenant_id,
                "candidate_build_id": candidate_build_id,
                "revision_id": change.candidate_revision_id,
                "change_set_id": change.change_set_id,
            },
        )
        await transition_agent_candidate_build(
            connection,
            tenant_id=tenant_id,
            candidate_build_id=candidate_build_id,
            expected=CandidateBuildStatus.BUILDING,
            target=CandidateBuildStatus.REVIEWABLE,
        )
        result_payload = {
            "seal_id": str(seal.seal_id),
            "candidate_build_id": str(candidate_build_id),
            "candidate_revision_id": str(change.candidate_revision_id),
            "change_set_id": str(change.change_set_id),
            "artifacts": [
                {
                    "artifact_id": str(item.artifact_id),
                    "upload_id": str(item.upload_id),
                    "filename": item.filename,
                    "artifact_kind": item.artifact_kind,
                    "object_key": item.object_key,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                    "content_type": item.content_type,
                }
                for item in committed
            ],
        }
        await connection.execute(
            text(
                """
                UPDATE agent_candidate_seals
                SET status='committed', result=CAST(:result AS jsonb),
                    error_code=NULL, completed_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=:tenant_id AND id=:seal_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "seal_id": seal.seal_id,
                "result": json.dumps(result_payload, separators=(",", ":")),
            },
        )
        await append_workflow_event(
            connection,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            event_type="agent.candidate.sealed",
            payload={
                "candidate_build_id": str(candidate_build_id),
                "candidate_revision_id": str(change.candidate_revision_id),
                "change_set_id": str(change.change_set_id),
                "artifact_ids": [str(item.artifact_id) for item in committed],
                "validation_status": validation_summary["status"],
                "risk_count": risk_summary["issue_count"],
            },
        )
        workflow_status = await connection.scalar(
            text("SELECT status FROM workflow_runs WHERE id=:id FOR UPDATE"),
            {"id": workflow_id},
        )
        if workflow_status == WorkflowStatus.RUNNING.value:
            await transition_workflow(
                connection,
                workflow_id,
                expected=WorkflowStatus.RUNNING,
                target=WorkflowStatus.SUCCEEDED,
            )
        elif workflow_status != WorkflowStatus.SUCCEEDED.value:
            raise CandidateBuildConflict(
                f"candidate cannot seal while workflow is {workflow_status}"
            )
    result = _seal_result_from_payload(result_payload, replayed=False)
    if fault_hook is not None:
        fault_hook("after_db_commit")
    for row in artifact_rows:
        try:
            await delete_object(row["staging_object_key"])
        except Exception:
            pass
    return result


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_filename(filename: str) -> str:
    name = filename.strip()
    if (
        not name
        or len(name.encode("utf-8")) > 255
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
        or PurePath(name).name != name
    ):
        raise ValueError("artifact filename must be one safe path segment")
    return name


def _assert_lease(
    row,
    *,
    lease_token: str,
    lease_generation: int,
    now: datetime,
    allow_completed: bool = False,
) -> None:
    stored_hash = row["lease_token_hash"] or ""
    supplied_hash = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
    if (
        row["lease_generation"] != lease_generation
        or not stored_hash
        or not hmac.compare_digest(stored_hash, supplied_hash)
    ):
        raise StaleLease("artifact commit lease token or generation is stale")
    if allow_completed and row["attempt_status"] == AttemptStatus.SUCCEEDED.value:
        return
    if row["leased_until"] is None or row["leased_until"] <= now:
        raise StaleLease("artifact commit lease has expired")
    if row["attempt_status"] not in {
        AttemptStatus.LEASED.value,
        AttemptStatus.RUNNING.value,
    }:
        raise IllegalTransition(
            f"attempt in {row['attempt_status']} cannot commit artifacts"
        )
    if row["cancellation_requested_at"] is not None or row[
        "workflow_status"
    ] in {
        WorkflowStatus.CANCELLING.value,
        WorkflowStatus.CANCELLED.value,
    }:
        raise IllegalTransition("workflow cancellation blocks artifact commit")


async def _locked_attempt_context(connection, attempt_id: UUID):
    return (
        await connection.execute(
            text(
                """
                SELECT a.tenant_id, a.workflow_run_id, a.status AS attempt_status,
                       a.lease_generation, a.lease_token_hash, a.leased_until,
                       w.project_id, w.kind AS workflow_kind,
                       w.request_payload, w.status AS workflow_status,
                       w.cancellation_requested_at
                FROM execution_attempts a
                JOIN workflow_runs w ON w.id=a.workflow_run_id
                WHERE a.id=:attempt_id
                FOR UPDATE OF a, w
                """
            ),
            {"attempt_id": attempt_id},
        )
    ).mappings().one_or_none()


async def _engineering_evidence_access(connection, attempt, revision_id, principal_id, kinds):
    """The only cross-workflow engineering outputs belong to a frozen task."""
    from types import SimpleNamespace
    from app.freecad.engineering_contracts import engineering_outputs, engineering_permissions
    from app.services.cloud_documents import authorized_document
    task_kind=(attempt['request_payload'].get('engineering_task') or {}).get('kind')
    if not set(kinds) <= set(engineering_outputs(task_kind)) | {"capability-result"}:
        raise IllegalTransition("undeclared engineering artifact kind")
    row = (await connection.execute(text('''SELECT t.document_id,t.task_kind,t.source_sha256,t.source_artifact_id,r.source_workflow_run_id
        FROM document_engineering_tasks t JOIN project_revisions r ON r.id=t.source_revision_id
        JOIN artifacts a ON a.id=t.source_artifact_id AND a.revision_id=t.source_revision_id AND a.sha256=t.source_sha256
        WHERE t.workflow_run_id=:workflow AND t.source_revision_id=:revision AND t.principal_id=:principal'''),
        {'workflow': attempt['workflow_run_id'], 'revision': revision_id, 'principal': principal_id})).mappings().one_or_none()
    request = dict(attempt['request_payload'] or {})
    if row is None or row['task_kind'] != task_kind or request.get('source_revision_id') != str(revision_id) or request.get('source_workflow_run_id') != str(row['source_workflow_run_id']) or request.get('source_artifact_id') != str(row['source_artifact_id']) or request.get('source_sha256') != row['source_sha256']:
        raise IllegalTransition("engineering artifact source does not match its immutable task")
    for permission in engineering_permissions(task_kind):
        await authorized_document(connection, SimpleNamespace(tenant_id=attempt['tenant_id'], principal_id=principal_id),
            row['document_id'], permission)


async def authorize_artifact_upload(
    *,
    tenant_id: UUID,
    principal_id: UUID,
    project_id: UUID,
    revision_id: UUID,
    attempt_id: UUID,
    lease_token: str,
    lease_generation: int,
    filename: str,
    artifact_kind: str,
    content_type: str,
    declared_size_bytes: int,
    declared_sha256: str,
    now: datetime | None = None,
) -> ArtifactUploadAuthorization:
    current_time = now or _utcnow()
    safe_name = _safe_filename(filename)
    kind = artifact_kind.strip().lower()
    if not _KIND_RE.fullmatch(kind):
        raise ValueError("artifact_kind is invalid")
    if declared_size_bytes < 1:
        raise ValueError("artifact upload must contain at least one byte")
    if declared_size_bytes > settings.artifact_max_upload_bytes:
        raise ValueError("artifact upload exceeds ARTIFACT_MAX_UPLOAD_BYTES")
    digest = declared_sha256.strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError("declared_sha256 must be a lowercase SHA-256 hex digest")
    content_type = content_type.strip()
    if not content_type or len(content_type) > 255:
        raise ValueError("content_type is invalid")

    upload_id = uuid4()
    expires_at = current_time + timedelta(seconds=settings.artifact_upload_ttl_s)
    async with tenant_transaction(tenant_id, principal_id) as connection:
        attempt = await _locked_attempt_context(connection, attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        if attempt["project_id"] != project_id:
            raise IllegalTransition("attempt does not belong to the requested project")
        _assert_lease(
            attempt,
            lease_token=lease_token,
            lease_generation=lease_generation,
            now=current_time,
        )
        revision_source = await connection.scalar(
            text(
                """
                SELECT source_workflow_run_id FROM project_revisions
                WHERE tenant_id=:tenant_id AND project_id=:project_id AND id=:revision_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "revision_id": revision_id,
            },
        )
        request_payload = dict(attempt["request_payload"] or {})
        is_declared_check_evidence = (
            attempt["workflow_kind"] == "mcad.check"
            and request_payload.get("source_revision_id")
            == str(revision_id)
            and request_payload.get("source_workflow_run_id")
            == str(revision_source)
            and kind == "dfm_report"
        )
        if attempt["workflow_kind"] == "mcad.engineering":
            await _engineering_evidence_access(connection, attempt, revision_id, principal_id, [kind])
            is_declared_check_evidence = True
        if (
            revision_source != attempt["workflow_run_id"]
            and not is_declared_check_evidence
        ):
            raise IllegalTransition(
                "revision is not owned by the attempt workflow"
            )
        staging_key = (
            f"staging/tenants/{tenant_id}/projects/{project_id}/"
            f"revisions/{revision_id}/attempts/{attempt_id}/"
            f"uploads/{upload_id}/{safe_name}"
        )
        await insert_upload_authorization(
            connection,
            upload_id=upload_id,
            tenant_id=tenant_id,
            project_id=project_id,
            revision_id=revision_id,
            workflow_id=attempt["workflow_run_id"],
            attempt_id=attempt_id,
            artifact_kind=kind,
            filename=safe_name,
            content_type=content_type,
            declared_size_bytes=declared_size_bytes,
            declared_sha256=digest,
            staging_object_key=staging_key,
            expires_at=expires_at,
        )

    signed = create_presigned_upload(
        staging_key,
        content_type=content_type,
        sha256=digest,
        expires_in=settings.artifact_upload_ttl_s,
    )
    return ArtifactUploadAuthorization(
        upload_id=upload_id,
        staging_object_key=staging_key,
        expires_at_iso=expires_at.isoformat(),
        required_headers=signed["headers"],
        upload_url=signed["url"],
    )


def _final_object_key(upload: dict) -> str:
    return (
        f"tenants/{upload['tenant_id']}/projects/{upload['project_id']}/"
        f"revisions/{upload['revision_id']}/attempts/{upload['attempt_id']}/"
        f"artifacts/{upload['declared_sha256']}/{upload['filename']}"
    )


def _completion_payload(
    revision_id: UUID,
    artifacts: list[CommittedArtifact] | tuple[CommittedArtifact, ...],
) -> dict[str, Any]:
    return {
        "status": "success",
        "revision_id": str(revision_id),
        "artifacts": [
            {
                "artifact_id": str(artifact.artifact_id),
                "filename": artifact.filename,
                "artifact_kind": artifact.artifact_kind,
                "object_key": artifact.object_key,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
                "content_type": artifact.content_type,
            }
            for artifact in artifacts
        ],
    }


async def _verify_staged_upload(upload: dict, *, now: datetime) -> None:
    if upload["status"] != "authorized":
        raise ArtifactVerificationError(
            f"upload {upload['id']} is not authorized"
        )
    if upload["expires_at"] <= now:
        raise ArtifactVerificationError(f"upload {upload['id']} has expired")
    try:
        metadata = await head_object(upload["staging_object_key"])
        digest = await sha256_object(upload["staging_object_key"])
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            raise ArtifactVerificationError(
                f"uploaded object {upload['filename']} is missing"
            ) from exc
        raise
    if metadata["size_bytes"] != upload["declared_size_bytes"]:
        raise ArtifactVerificationError(
            f"uploaded object {upload['filename']} has an invalid size"
        )
    if metadata["metadata"].get("sha256") != upload["declared_sha256"]:
        raise ArtifactVerificationError(
            f"uploaded object {upload['filename']} is missing signed SHA-256 metadata"
        )
    if digest["size_bytes"] != upload["declared_size_bytes"]:
        raise ArtifactVerificationError(
            f"uploaded object {upload['filename']} changed during verification"
        )
    if digest["sha256"] != upload["declared_sha256"]:
        raise ArtifactVerificationError(
            f"uploaded object {upload['filename']} failed SHA-256 verification"
        )


async def _reject_after_failure(
    *,
    tenant_id: UUID,
    principal_id: UUID,
    upload_ids: list[UUID],
    rejection_code: str,
) -> None:
    async with tenant_transaction(tenant_id, principal_id) as connection:
        workflow_id = await reject_uploads(
            connection,
            upload_ids=upload_ids,
            rejection_code=rejection_code,
        )
        if workflow_id is not None:
            await append_workflow_event(
                connection,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                event_type="artifact.rejected",
                payload={
                    "upload_ids": [str(upload_id) for upload_id in upload_ids],
                    "rejection_code": rejection_code,
                },
            )


async def commit_artifacts(
    *,
    tenant_id: UUID,
    principal_id: UUID,
    attempt_id: UUID,
    revision_id: UUID,
    upload_ids: list[UUID],
    lease_token: str,
    lease_generation: int,
    runtime_metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ArtifactCommitResult:
    current_time = now or _utcnow()
    unique_upload_ids = list(dict.fromkeys(upload_ids))
    if not unique_upload_ids or len(unique_upload_ids) != len(upload_ids):
        raise ValueError("upload_ids must be a non-empty unique list")
    runtime_metadata = runtime_metadata or {}
    copied_keys: list[str] = []
    staging_keys: list[str] = []
    try:
        async with tenant_transaction(tenant_id, principal_id) as connection:
            attempt = await _locked_attempt_context(connection, attempt_id)
            if attempt is None:
                raise KeyError(attempt_id)
            _assert_lease(
                attempt,
                lease_token=lease_token,
                lease_generation=lease_generation,
                now=current_time,
                allow_completed=True,
            )
            uploads = await locked_uploads(
                connection,
                upload_ids=unique_upload_ids,
            )
            if len(uploads) != len(unique_upload_ids):
                raise ArtifactVerificationError(
                    "one or more artifact upload authorizations do not exist"
                )
            if attempt["workflow_kind"] == "mcad.engineering":
                await _engineering_evidence_access(connection, attempt, revision_id, principal_id,
                    [upload['artifact_kind'] for upload in uploads])
            for upload in uploads:
                if (
                    upload["tenant_id"] != tenant_id
                    or upload["attempt_id"] != attempt_id
                    or upload["revision_id"] != revision_id
                    or upload["workflow_run_id"] != attempt["workflow_run_id"]
                    or upload["project_id"] != attempt["project_id"]
                ):
                    raise ArtifactVerificationError(
                        "artifact upload ownership does not match attempt and revision"
                    )

            if attempt["attempt_status"] == AttemptStatus.SUCCEEDED.value:
                existing = await artifacts_for_uploads(
                    connection,
                    upload_ids=unique_upload_ids,
                )
                if len(existing) != len(unique_upload_ids):
                    raise IdempotencyConflict(
                        "completed attempt artifact replay does not match committed rows"
                    )
                completion = await complete_attempt(
                    connection,
                    attempt_id,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                    result_payload=_completion_payload(revision_id, existing),
                    now=current_time,
                )
                return ArtifactCommitResult(
                    attempt_id=attempt_id,
                    revision_id=revision_id,
                    artifacts=tuple(existing),
                    replayed=completion.replayed,
                )

            if len({upload["filename"] for upload in uploads}) != len(uploads):
                raise ArtifactVerificationError(
                    "artifact filenames must be unique inside one revision"
                )
            for upload in uploads:
                await _verify_staged_upload(upload, now=current_time)
                staging_keys.append(upload["staging_object_key"])

            committed_artifacts: list[CommittedArtifact] = []
            try:
                for upload in uploads:
                    final_key = _final_object_key(upload)
                    await copy_object(upload["staging_object_key"], final_key)
                    copied_keys.append(final_key)
                    committed_artifacts.append(
                        await insert_artifact(
                            connection,
                            artifact_id=uuid4(),
                            upload=upload,
                            object_key=final_key,
                            runtime_metadata=runtime_metadata,
                        )
                    )
                await mark_uploads_committed(
                    connection,
                    upload_ids=unique_upload_ids,
                    committed_at=current_time,
                )
                await append_workflow_event(
                    connection,
                    tenant_id=tenant_id,
                    workflow_id=attempt["workflow_run_id"],
                    event_type="artifact.committed",
                    payload={
                        "attempt_id": str(attempt_id),
                        "revision_id": str(revision_id),
                        "artifact_ids": [
                            str(artifact.artifact_id)
                            for artifact in committed_artifacts
                        ],
                    },
                )
                completion = await complete_attempt(
                    connection,
                    attempt_id,
                    lease_token=lease_token,
                    lease_generation=lease_generation,
                    result_payload=_completion_payload(
                        revision_id,
                        committed_artifacts,
                    ),
                    now=current_time,
                )
            except Exception:
                for copied_key in copied_keys:
                    try:
                        await delete_object(copied_key)
                    except Exception:
                        pass
                raise
        for staging_key in staging_keys:
            try:
                await delete_object(staging_key)
            except Exception:
                pass
        return ArtifactCommitResult(
            attempt_id=attempt_id,
            revision_id=revision_id,
            artifacts=tuple(committed_artifacts),
            replayed=completion.replayed,
        )
    except ArtifactVerificationError:
        await _reject_after_failure(
            tenant_id=tenant_id,
            principal_id=principal_id,
            upload_ids=unique_upload_ids,
            rejection_code="verification_failed",
        )
        raise
    except StaleLease:
        await _reject_after_failure(
            tenant_id=tenant_id,
            principal_id=principal_id,
            upload_ids=unique_upload_ids,
            rejection_code="stale_lease",
        )
        raise
    except IllegalTransition:
        await _reject_after_failure(
            tenant_id=tenant_id,
            principal_id=principal_id,
            upload_ids=unique_upload_ids,
            rejection_code="workflow_not_committable",
        )
        raise


async def cleanup_artifact_orphans(
    *,
    tenant_id: UUID,
    principal_id: UUID,
    now: datetime | None = None,
    grace_seconds: int | None = None,
) -> dict[str, int]:
    """Delete expired staging bytes and unreferenced final objects for one tenant."""
    current_time = now or _utcnow()
    grace = (
        settings.artifact_orphan_grace_s
        if grace_seconds is None
        else max(0, grace_seconds)
    )
    cutoff = current_time - timedelta(seconds=grace)
    async with tenant_transaction(tenant_id, principal_id) as connection:
        staging_rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, staging_object_key FROM artifact_uploads
                    WHERE tenant_id=:tenant_id
                      AND (
                        status IN ('committed', 'rejected', 'expired')
                        OR (status='authorized' AND expires_at <= :now)
                      )
                    """
                ),
                {"tenant_id": tenant_id, "now": current_time},
            )
        ).mappings().all()
        await connection.execute(
            text(
                """
                UPDATE artifact_uploads
                SET status='expired', rejection_code='authorization_expired'
                WHERE tenant_id=:tenant_id
                  AND status='authorized'
                  AND expires_at <= :now
                """
            ),
            {"tenant_id": tenant_id, "now": current_time},
        )
        committed_keys = set(
            (
                await connection.execute(
                    text(
                        "SELECT object_key FROM artifacts "
                        "WHERE tenant_id=:tenant_id"
                    ),
                    {"tenant_id": tenant_id},
                )
            ).scalars()
        )
        registered_staging_keys = set(
            (
                await connection.execute(
                    text(
                        "SELECT staging_object_key FROM artifact_uploads "
                        "WHERE tenant_id=:tenant_id"
                    ),
                    {"tenant_id": tenant_id},
                )
            ).scalars()
        )
        terminal_agent_rows = (
            await connection.execute(
                text(
                    """
                    SELECT m.manifest, e.evidence
                    FROM agent_candidate_builds b
                    JOIN agent_staging_manifests m
                      ON m.candidate_build_id=b.id
                    LEFT JOIN agent_validation_evidence e
                      ON e.staging_manifest_id=m.id
                    WHERE b.tenant_id=:tenant_id
                      AND b.status IN (
                        'reviewable', 'failed', 'cancelled', 'abandoned'
                      )
                      AND b.completed_at IS NOT NULL
                      AND b.completed_at <= :cutoff
                    """
                ),
                {"tenant_id": tenant_id, "cutoff": cutoff},
            )
        ).mappings().all()
        active_agent_rows = (
            await connection.execute(
                text(
                    """
                    SELECT m.manifest, e.evidence
                    FROM agent_candidate_builds b
                    JOIN agent_staging_manifests m
                      ON m.candidate_build_id=b.id
                    LEFT JOIN agent_validation_evidence e
                      ON e.staging_manifest_id=m.id
                    WHERE b.tenant_id=:tenant_id AND b.status='building'
                    """
                ),
                {"tenant_id": tenant_id},
            )
        ).mappings().all()

    def agent_keys(rows) -> set[str]:
        keys: set[str] = set()
        for row in rows:
            manifest = dict(row["manifest"] or {})
            keys.update(
                str(item["object_key"])
                for item in manifest.get("outputs") or ()
                if item.get("object_key")
            )
            evidence = dict(row["evidence"] or {})
            keys.update(
                str(item["object_key"])
                for item in evidence.get("renders") or ()
                if item.get("object_key")
            )
            report_artifact = dict(evidence.get("report_artifact") or {})
            if report_artifact.get("object_key"):
                keys.add(str(report_artifact["object_key"]))
        return keys

    terminal_agent_keys = agent_keys(terminal_agent_rows)
    active_agent_keys = agent_keys(active_agent_rows)

    deleted_staging = 0
    for row in staging_rows:
        try:
            await delete_object(row["staging_object_key"])
            deleted_staging += 1
        except Exception:
            pass

    staging_prefix = f"staging/tenants/{tenant_id}/"
    for item in await list_objects(staging_prefix):
        last_modified = item.get("last_modified")
        if (
            item["key"] not in registered_staging_keys
            and last_modified is not None
            and (grace == 0 or last_modified <= cutoff)
        ):
            try:
                await delete_object(item["key"])
                deleted_staging += 1
            except Exception:
                pass

    for key in terminal_agent_keys:
        try:
            await delete_object(key)
            deleted_staging += 1
        except Exception:
            pass

    agent_staging_prefix = f"staging/agent/tenants/{tenant_id}/"
    for item in await list_objects(agent_staging_prefix):
        last_modified = item.get("last_modified")
        if (
            item["key"] not in active_agent_keys
            and last_modified is not None
            and (grace == 0 or last_modified <= cutoff)
        ):
            try:
                await delete_object(item["key"])
                deleted_staging += 1
            except Exception:
                pass

    deleted_final_orphans = 0
    final_prefix = f"tenants/{tenant_id}/"
    for item in await list_objects(final_prefix):
        last_modified = item.get("last_modified")
        if (
            item["key"] not in committed_keys
            and last_modified is not None
            and (grace == 0 or last_modified <= cutoff)
        ):
            try:
                await delete_object(item["key"])
                deleted_final_orphans += 1
            except Exception:
                pass
    return {
        "deleted_staging_objects": deleted_staging,
        "deleted_final_orphans": deleted_final_orphans,
    }
