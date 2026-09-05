"""PostgreSQL metadata operations for two-phase artifact commit."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.artifacts import CommittedArtifact


async def committed_artifact_for_revision(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    revision_id: UUID,
    artifact_kind: str,
) -> dict[str, Any] | None:
    """Resolve one immutable revision artifact by kind, failing on ambiguity."""
    rows = (
        await connection.execute(
            text(
                """
                SELECT id, revision_id, workflow_run_id, attempt_id,
                       artifact_kind, filename, content_type, size_bytes,
                       sha256, object_key, runtime_metadata, created_at
                FROM artifacts
                WHERE tenant_id=:tenant_id AND project_id=:project_id
                  AND revision_id=:revision_id
                  AND lower(artifact_kind)=lower(:artifact_kind)
                ORDER BY created_at DESC, id DESC
                LIMIT 2
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "revision_id": revision_id,
                "artifact_kind": artifact_kind,
            },
        )
    ).mappings().all()
    if len(rows) > 1:
        raise ValueError(
            f"revision has multiple {artifact_kind!r} artifacts"
        )
    return dict(rows[0]) if rows else None


async def insert_upload_authorization(
    connection: AsyncConnection,
    *,
    upload_id: UUID,
    tenant_id: UUID,
    project_id: UUID,
    revision_id: UUID,
    workflow_id: UUID,
    attempt_id: UUID,
    artifact_kind: str,
    filename: str,
    content_type: str,
    declared_size_bytes: int,
    declared_sha256: str,
    staging_object_key: str,
    expires_at: datetime,
) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO artifact_uploads (
                id, tenant_id, project_id, revision_id, workflow_run_id,
                attempt_id, artifact_kind, filename, content_type,
                declared_size_bytes, declared_sha256, staging_object_key,
                expires_at
            )
            VALUES (
                :id, :tenant_id, :project_id, :revision_id, :workflow_id,
                :attempt_id, :artifact_kind, :filename, :content_type,
                :declared_size_bytes, :declared_sha256, :staging_object_key,
                :expires_at
            )
            """
        ),
        {
            "id": upload_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "revision_id": revision_id,
            "workflow_id": workflow_id,
            "attempt_id": attempt_id,
            "artifact_kind": artifact_kind,
            "filename": filename,
            "content_type": content_type,
            "declared_size_bytes": declared_size_bytes,
            "declared_sha256": declared_sha256,
            "staging_object_key": staging_object_key,
            "expires_at": expires_at,
        },
    )


async def locked_uploads(
    connection: AsyncConnection,
    *,
    upload_ids: list[UUID],
) -> list[dict]:
    return [
        dict(row)
        for row in (
            await connection.execute(
                text(
                    """
                    SELECT * FROM artifact_uploads
                    WHERE id = ANY(:upload_ids)
                    ORDER BY filename, id
                    FOR UPDATE
                    """
                ),
                {"upload_ids": upload_ids},
            )
        ).mappings().all()
    ]


async def insert_artifact(
    connection: AsyncConnection,
    *,
    artifact_id: UUID,
    upload: dict,
    object_key: str,
    runtime_metadata: dict[str, Any],
) -> CommittedArtifact:
    await connection.execute(
        text(
            """
            INSERT INTO artifacts (
                id, tenant_id, project_id, revision_id, workflow_run_id,
                attempt_id, upload_id, artifact_kind, filename, content_type,
                size_bytes, sha256, object_key, runtime_metadata
            )
            VALUES (
                :id, :tenant_id, :project_id, :revision_id, :workflow_id,
                :attempt_id, :upload_id, :artifact_kind, :filename, :content_type,
                :size_bytes, :sha256, :object_key, CAST(:runtime_metadata AS jsonb)
            )
            """
        ),
        {
            "id": artifact_id,
            "tenant_id": upload["tenant_id"],
            "project_id": upload["project_id"],
            "revision_id": upload["revision_id"],
            "workflow_id": upload["workflow_run_id"],
            "attempt_id": upload["attempt_id"],
            "upload_id": upload["id"],
            "artifact_kind": upload["artifact_kind"],
            "filename": upload["filename"],
            "content_type": upload["content_type"],
            "size_bytes": upload["declared_size_bytes"],
            "sha256": upload["declared_sha256"],
            "object_key": object_key,
            "runtime_metadata": json.dumps(
                runtime_metadata,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        },
    )
    return CommittedArtifact(
        artifact_id=artifact_id,
        upload_id=upload["id"],
        filename=upload["filename"],
        artifact_kind=upload["artifact_kind"],
        object_key=object_key,
        size_bytes=int(upload["declared_size_bytes"]),
        sha256=upload["declared_sha256"],
        content_type=upload["content_type"],
    )


async def mark_uploads_committed(
    connection: AsyncConnection,
    *,
    upload_ids: Iterable[UUID],
    committed_at: datetime,
) -> None:
    await connection.execute(
        text(
            """
            UPDATE artifact_uploads
            SET status='committed', committed_at=:committed_at,
                rejection_code=NULL
            WHERE id = ANY(:upload_ids)
            """
        ),
        {"upload_ids": list(upload_ids), "committed_at": committed_at},
    )


async def artifacts_for_uploads(
    connection: AsyncConnection,
    *,
    upload_ids: list[UUID],
) -> list[CommittedArtifact]:
    rows = (
        await connection.execute(
            text(
                """
                SELECT id, upload_id, filename, artifact_kind, object_key,
                       size_bytes, sha256, content_type
                FROM artifacts
                WHERE upload_id = ANY(:upload_ids)
                ORDER BY filename, id
                """
            ),
            {"upload_ids": upload_ids},
        )
    ).mappings().all()
    return [
        CommittedArtifact(
            artifact_id=row["id"],
            upload_id=row["upload_id"],
            filename=row["filename"],
            artifact_kind=row["artifact_kind"],
            object_key=row["object_key"],
            size_bytes=int(row["size_bytes"]),
            sha256=row["sha256"],
            content_type=row["content_type"],
        )
        for row in rows
    ]


async def reject_uploads(
    connection: AsyncConnection,
    *,
    upload_ids: list[UUID],
    rejection_code: str,
) -> UUID | None:
    workflow_id = await connection.scalar(
        text(
            "SELECT workflow_run_id FROM artifact_uploads "
            "WHERE id = ANY(:upload_ids) LIMIT 1"
        ),
        {"upload_ids": upload_ids},
    )
    await connection.execute(
        text(
            """
            UPDATE artifact_uploads
            SET status='rejected', rejection_code=:rejection_code
            WHERE id = ANY(:upload_ids) AND status='authorized'
            """
        ),
        {"upload_ids": upload_ids, "rejection_code": rejection_code},
    )
    return workflow_id
