"""Two-phase S3 artifact authorization, verification, and atomic DB commit."""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import Any
from uuid import UUID, uuid4

from botocore.exceptions import ClientError
from sqlalchemy import text

from app.config import settings
from app.db import tenant_transaction
from app.domain.artifacts import (
    ArtifactCommitResult,
    ArtifactUploadAuthorization,
    CommittedArtifact,
)
from app.domain.runs import AttemptStatus, WorkflowStatus
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
from app.services.run_state import (
    IdempotencyConflict,
    IllegalTransition,
    StaleLease,
    complete_attempt,
)


class ArtifactVerificationError(RuntimeError):
    """Uploaded bytes do not match their immutable authorization."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


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
            and last_modified <= cutoff
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
            and last_modified <= cutoff
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
