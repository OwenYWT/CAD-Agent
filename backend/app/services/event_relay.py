"""Authorized durable task snapshots, event replay, and revision reads.

WebSocket delivery deliberately reads this persisted event log.  It is a
projection/transport concern only: workflow lifetime never depends on a socket
or an API process remaining alive.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.identity import PrincipalContext
from app.domain.projects import Permission
from app.domain.runs import WorkflowStatus
from app.object_store import presign_get
from app.repositories.projects import principal_has_permission


class CursorExpired(RuntimeError):
    def __init__(self, earliest_sequence: int, current_sequence: int) -> None:
        self.earliest_sequence = earliest_sequence
        self.current_sequence = current_sequence
        super().__init__(
            "事件游标早于当前保留范围；"
            f"最早序号={earliest_sequence}，当前序号={current_sequence}"
        )


async def _require_project_permission(
    connection,
    *,
    context: PrincipalContext,
    project_id: UUID,
    permission: Permission,
) -> None:
    if not await principal_has_permission(
        connection,
        tenant_id=context.tenant_id,
        project_id=project_id,
        principal_id=context.principal_id,
        permission=permission,
    ):
        raise PermissionError(
            f"principal lacks {permission.value} permission for this project"
        )


async def workflow_project_id(
    context: PrincipalContext,
    workflow_run_id: UUID,
    *,
    permission: Permission,
) -> UUID:
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        project_id = await connection.scalar(
            text(
                """
                SELECT project_id FROM workflow_runs
                WHERE tenant_id=:tenant_id AND id=:workflow_run_id
                """
            ),
            {
                "tenant_id": context.tenant_id,
                "workflow_run_id": workflow_run_id,
            },
        )
        if project_id is None:
            raise KeyError(workflow_run_id)
        await _require_project_permission(
            connection,
            context=context,
            project_id=project_id,
            permission=permission,
        )
        return project_id


async def get_task_snapshot(
    context: PrincipalContext,
    workflow_run_id: UUID,
) -> dict[str, Any]:
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        workflow = (
            await connection.execute(
                text(
                    """
                    SELECT id, project_id, requested_by_principal_id, kind,
                           status, request_payload, last_event_sequence,
                           cancellation_requested_at, error_code, error_message,
                           created_at, started_at, updated_at, completed_at
                    FROM workflow_runs
                    WHERE tenant_id=:tenant_id AND id=:workflow_run_id
                    """
                ),
                {
                    "tenant_id": context.tenant_id,
                    "workflow_run_id": workflow_run_id,
                },
            )
        ).mappings().one_or_none()
        if workflow is None:
            raise KeyError(workflow_run_id)
        await _require_project_permission(
            connection,
            context=context,
            project_id=workflow["project_id"],
            permission=Permission.VIEW_PROJECT,
        )
        step_rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, step_key, step_index, kind, status,
                           attempt_count, error_code, error_message
                    FROM step_runs
                    WHERE workflow_run_id=:workflow_run_id
                    ORDER BY step_index, created_at
                    """
                ),
                {"workflow_run_id": workflow_run_id},
            )
        ).mappings().all()
        attempt_rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, step_run_id, attempt_number, status, worker_id,
                           error_code, error_message, started_at, completed_at
                    FROM execution_attempts
                    WHERE workflow_run_id=:workflow_run_id
                    ORDER BY step_run_id, attempt_number
                    """
                ),
                {"workflow_run_id": workflow_run_id},
            )
        ).mappings().all()
        artifact_rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, revision_id, artifact_kind, filename,
                           content_type, size_bytes, sha256, created_at
                    FROM artifacts
                    WHERE workflow_run_id=:workflow_run_id
                    ORDER BY created_at, filename
                    """
                ),
                {"workflow_run_id": workflow_run_id},
            )
        ).mappings().all()
        change_set = (
            await connection.execute(
                text(
                    """
                    SELECT id, status, base_revision_id,
                           candidate_revision_id, objective, updated_at
                    FROM change_sets
                    WHERE source_workflow_run_id=:workflow_run_id
                    """
                ),
                {"workflow_run_id": workflow_run_id},
            )
        ).mappings().one_or_none()

    attempts_by_step: dict[UUID, list[dict[str, Any]]] = {}
    for row in attempt_rows:
        attempts_by_step.setdefault(row["step_run_id"], []).append(dict(row))
    steps = []
    for row in step_rows:
        item = dict(row)
        item["attempts"] = attempts_by_step.get(row["id"], [])
        steps.append(item)
    return {
        **dict(workflow),
        "steps": steps,
        "artifacts": [
            {
                **dict(row),
                "download_url": (
                    f"/api/files/{workflow_run_id}/{row['filename']}"
                ),
            }
            for row in artifact_rows
        ],
        "change_set": dict(change_set) if change_set else None,
    }


async def read_task_events(
    context: PrincipalContext,
    workflow_run_id: UUID,
    *,
    after_sequence: int,
    limit: int,
) -> dict[str, Any]:
    if after_sequence < 0:
        raise ValueError("after_sequence must be non-negative")
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        workflow = (
            await connection.execute(
                text(
                    """
                    SELECT project_id, last_event_sequence
                    FROM workflow_runs
                    WHERE tenant_id=:tenant_id AND id=:workflow_run_id
                    """
                ),
                {
                    "tenant_id": context.tenant_id,
                    "workflow_run_id": workflow_run_id,
                },
            )
        ).mappings().one_or_none()
        if workflow is None:
            raise KeyError(workflow_run_id)
        await _require_project_permission(
            connection,
            context=context,
            project_id=workflow["project_id"],
            permission=Permission.VIEW_PROJECT,
        )
        current_sequence = int(workflow["last_event_sequence"])
        if after_sequence > current_sequence:
            raise ValueError(
                "after_sequence cannot be newer than the workflow event log"
            )
        earliest = await connection.scalar(
            text(
                """
                SELECT min(sequence) FROM task_events
                WHERE workflow_run_id=:workflow_run_id
                """
            ),
            {"workflow_run_id": workflow_run_id},
        )
        earliest_sequence = int(earliest or (current_sequence + 1))
        if after_sequence < earliest_sequence - 1:
            raise CursorExpired(earliest_sequence, current_sequence)
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, workflow_run_id, sequence, event_type,
                           payload, occurred_at
                    FROM task_events
                    WHERE workflow_run_id=:workflow_run_id
                      AND sequence>:after_sequence
                    ORDER BY sequence
                    LIMIT :limit
                    """
                ),
                {
                    "workflow_run_id": workflow_run_id,
                    "after_sequence": after_sequence,
                    "limit": limit,
                },
            )
        ).mappings().all()
    events = [dict(row) for row in rows]
    next_cursor = int(events[-1]["sequence"]) if events else after_sequence
    return {
        "workflow_run_id": workflow_run_id,
        "events": events,
        "after_sequence": after_sequence,
        "next_cursor": next_cursor,
        "earliest_sequence": earliest_sequence,
        "current_sequence": current_sequence,
        "has_more": next_cursor < current_sequence,
    }


async def get_change_set_detail(
    context: PrincipalContext,
    change_set_id: UUID,
) -> dict[str, Any]:
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT c.*, b.name AS branch_name,
                           b.head_revision_id,
                           base.revision_number AS base_revision_number,
                           base.content_hash AS base_content_hash,
                           base.manifest AS base_manifest,
                           candidate.revision_number AS candidate_revision_number,
                           candidate.content_hash AS candidate_content_hash,
                           candidate.manifest AS candidate_manifest,
                           w.status AS workflow_status
                    FROM change_sets c
                    JOIN project_branches b ON b.id=c.branch_id
                    JOIN project_revisions base ON base.id=c.base_revision_id
                    JOIN project_revisions candidate
                      ON candidate.id=c.candidate_revision_id
                    LEFT JOIN workflow_runs w ON w.id=c.source_workflow_run_id
                    WHERE c.tenant_id=:tenant_id AND c.id=:change_set_id
                    """
                ),
                {
                    "tenant_id": context.tenant_id,
                    "change_set_id": change_set_id,
                },
            )
        ).mappings().one_or_none()
        if row is None:
            raise KeyError(change_set_id)
        await _require_project_permission(
            connection,
            context=context,
            project_id=row["project_id"],
            permission=Permission.VIEW_PROJECT,
        )
        artifact_rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, revision_id, artifact_kind, filename,
                           content_type, size_bytes, sha256, object_key,
                           created_at
                    FROM artifacts
                    WHERE revision_id IN (
                        :base_revision_id, :candidate_revision_id
                    )
                    ORDER BY revision_id, created_at, filename
                    """
                ),
                {
                    "base_revision_id": row["base_revision_id"],
                    "candidate_revision_id": row["candidate_revision_id"],
                },
            )
        ).mappings().all()
        audit_rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, actor_principal_id, action, payload, occurred_at
                    FROM audit_records
                    WHERE target_type='change_set' AND target_id=:target_id
                    ORDER BY occurred_at, id
                    """
                ),
                {"target_id": str(change_set_id)},
            )
        ).mappings().all()
    artifacts = []
    for artifact in artifact_rows:
        public_artifact = {
            key: value
            for key, value in dict(artifact).items()
            if key != "object_key"
        }
        public_artifact["download_url"] = presign_get(artifact["object_key"])
        artifacts.append(public_artifact)
    public_fields = (
        "id",
        "project_id",
        "branch_id",
        "branch_name",
        "base_revision_id",
        "base_revision_number",
        "base_content_hash",
        "base_manifest",
        "candidate_revision_id",
        "candidate_revision_number",
        "candidate_content_hash",
        "candidate_manifest",
        "source_workflow_run_id",
        "objective",
        "change_summary",
        "validation_summary",
        "risk_summary",
        "status",
        "workflow_status",
        "created_at",
        "updated_at",
        "reviewed_at",
        "review_note",
        "accepted_at",
        "committed_at",
        "rolled_back_at",
    )
    item = {field: row[field] for field in public_fields}
    item["base_artifacts"] = [
        artifact
        for artifact in artifacts
        if artifact["revision_id"] == row["base_revision_id"]
    ]
    item["candidate_artifacts"] = [
        artifact
        for artifact in artifacts
        if artifact["revision_id"] == row["candidate_revision_id"]
    ]
    # Compatibility alias for clients that only need the candidate outputs.
    item["artifacts"] = item["candidate_artifacts"]
    item["audit_log"] = [dict(audit) for audit in audit_rows]
    return item


async def change_set_workflow_id(
    context: PrincipalContext,
    change_set_id: UUID,
    *,
    permission: Permission = Permission.REVIEW_CHANGE,
) -> UUID | None:
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT c.project_id, c.source_workflow_run_id,
                           w.status AS workflow_status
                    FROM change_sets c
                    LEFT JOIN workflow_runs w
                      ON w.id=c.source_workflow_run_id
                    WHERE c.tenant_id=:tenant_id AND c.id=:change_set_id
                    """
                ),
                {
                    "tenant_id": context.tenant_id,
                    "change_set_id": change_set_id,
                },
            )
        ).mappings().one_or_none()
        if row is None:
            raise KeyError(change_set_id)
        await _require_project_permission(
            connection,
            context=context,
            project_id=row["project_id"],
            permission=permission,
        )
        # Temporal buffers signals received just before the workflow starts
        # awaiting confirmation. Include that active window so a review cannot
        # be persisted without waking the workflow. Closed/cancelling histories
        # remain reviewable but must not receive another signal.
        signalable = {
            WorkflowStatus.PENDING.value,
            WorkflowStatus.PLANNING.value,
            WorkflowStatus.RUNNING.value,
            WorkflowStatus.WAITING_CONFIRMATION.value,
        }
        if row["workflow_status"] not in signalable:
            return None
        return row["source_workflow_run_id"]


async def list_project_branches(
    context: PrincipalContext,
    project_id: UUID,
) -> list[dict[str, Any]]:
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await _require_project_permission(
            connection,
            context=context,
            project_id=project_id,
            permission=Permission.VIEW_PROJECT,
        )
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT b.id, b.project_id, b.name, b.head_revision_id,
                           b.next_revision_number, b.created_at, b.updated_at,
                           r.revision_number AS head_revision_number
                    FROM project_branches b
                    LEFT JOIN project_revisions r ON r.id=b.head_revision_id
                    WHERE b.project_id=:project_id
                    ORDER BY b.updated_at DESC, b.name
                    """
                ),
                {"project_id": project_id},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def list_branch_revisions(
    context: PrincipalContext,
    project_id: UUID,
    branch_id: UUID,
) -> list[dict[str, Any]]:
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await _require_project_permission(
            connection,
            context=context,
            project_id=project_id,
            permission=Permission.VIEW_PROJECT,
        )
        branch_exists = await connection.scalar(
            text(
                """
                SELECT 1 FROM project_branches
                WHERE project_id=:project_id AND id=:branch_id
                """
            ),
            {"project_id": project_id, "branch_id": branch_id},
        )
        if branch_exists is None:
            raise KeyError(branch_id)
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, project_id, branch_id, parent_revision_id,
                           revision_number, kind, content_hash,
                           source_workflow_run_id, created_by_principal_id,
                           created_at
                    FROM project_revisions
                    WHERE project_id=:project_id AND branch_id=:branch_id
                    ORDER BY revision_number DESC
                    """
                ),
                {"project_id": project_id, "branch_id": branch_id},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def get_revision_detail(
    context: PrincipalContext,
    project_id: UUID,
    revision_id: UUID,
) -> dict[str, Any]:
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await _require_project_permission(
            connection,
            context=context,
            project_id=project_id,
            permission=Permission.VIEW_PROJECT,
        )
        revision = (
            await connection.execute(
                text(
                    """
                    SELECT id, project_id, branch_id, parent_revision_id,
                           revision_number, kind, content_hash, manifest,
                           source_workflow_run_id, created_by_principal_id,
                           created_at
                    FROM project_revisions
                    WHERE project_id=:project_id AND id=:revision_id
                    """
                ),
                {"project_id": project_id, "revision_id": revision_id},
            )
        ).mappings().one_or_none()
        if revision is None:
            raise KeyError(revision_id)
        artifacts = (
            await connection.execute(
                text(
                    """
                    SELECT id, artifact_kind, filename, content_type, size_bytes,
                           sha256, object_key, runtime_metadata, created_at
                    FROM artifacts
                    WHERE project_id=:project_id AND revision_id=:revision_id
                    ORDER BY created_at, filename
                    """
                ),
                {"project_id": project_id, "revision_id": revision_id},
            )
        ).mappings().all()
    result = dict(revision)
    result["artifacts"] = []
    for artifact in artifacts:
        public_artifact = {
            key: value
            for key, value in dict(artifact).items()
            if key != "object_key"
        }
        public_artifact["download_url"] = presign_get(artifact["object_key"])
        result["artifacts"].append(public_artifact)
    return result
