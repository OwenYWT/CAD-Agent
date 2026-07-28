"""PostgreSQL revision, branch, and candidate Change Set persistence."""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.revisions import (
    CandidateChangeSetCreated,
    InitialBranchCreated,
)
from app.execution.canonical import canonical_sha256


class StaleBaseRevision(RuntimeError):
    """The requested base is no longer the branch head."""


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


async def create_initial_branch(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    created_by_principal_id: UUID,
    branch_name: str,
    initial_manifest: dict[str, Any],
    source_workflow_run_id: UUID | None = None,
) -> InitialBranchCreated:
    content_hash = canonical_sha256(initial_manifest)
    project_exists = await connection.scalar(
        text(
            "SELECT id FROM projects "
            "WHERE tenant_id=:tenant_id AND id=:project_id FOR UPDATE"
        ),
        {"tenant_id": tenant_id, "project_id": project_id},
    )
    if project_exists is None:
        raise KeyError(project_id)
    existing = (
        await connection.execute(
            text(
                """
                SELECT b.id AS branch_id, r.id AS initial_revision_id,
                       r.revision_number,
                       r.content_hash, r.created_by_principal_id
                FROM project_branches b
                JOIN project_revisions r
                  ON r.branch_id=b.id AND r.revision_number=1
                WHERE b.tenant_id=:tenant_id
                  AND b.project_id=:project_id
                  AND b.name=:branch_name
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "branch_name": branch_name,
            },
        )
    ).mappings().one_or_none()
    if existing:
        if (
            existing["content_hash"] != content_hash
            or existing["created_by_principal_id"] != created_by_principal_id
            or existing["revision_number"] != 1
        ):
            raise ValueError(
                "initial branch already exists with different immutable content"
            )
        return InitialBranchCreated(
            branch_id=existing["branch_id"],
            revision_id=existing["initial_revision_id"],
            revision_number=existing["revision_number"],
            replayed=True,
        )

    branch_id = uuid4()
    revision_id = uuid4()
    await connection.execute(
        text(
            """
            INSERT INTO project_branches (
                id, tenant_id, project_id, name, next_revision_number,
                created_by_principal_id
            )
            VALUES (
                :id, :tenant_id, :project_id, :name, 2, :creator
            )
            """
        ),
        {
            "id": branch_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "name": branch_name,
            "creator": created_by_principal_id,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO project_revisions (
                id, tenant_id, project_id, branch_id, parent_revision_id,
                revision_number, kind, content_hash, manifest,
                source_workflow_run_id, created_by_principal_id
            )
            VALUES (
                :id, :tenant_id, :project_id, :branch_id, NULL,
                1, 'initial', :content_hash, CAST(:manifest AS jsonb),
                :source_workflow_id, :creator
            )
            """
        ),
        {
            "id": revision_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "branch_id": branch_id,
            "content_hash": content_hash,
            "manifest": _json(initial_manifest),
            "source_workflow_id": source_workflow_run_id,
            "creator": created_by_principal_id,
        },
    )
    await connection.execute(
        text(
            """
            UPDATE project_branches
            SET head_revision_id=:revision_id, updated_at=CURRENT_TIMESTAMP
            WHERE id=:branch_id
            """
        ),
        {"branch_id": branch_id, "revision_id": revision_id},
    )
    return InitialBranchCreated(
        branch_id=branch_id,
        revision_id=revision_id,
        revision_number=1,
    )


async def create_candidate_change_set(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    branch_id: UUID,
    expected_base_revision_id: UUID,
    created_by_principal_id: UUID,
    idempotency_key: str,
    objective: str,
    candidate_manifest: dict[str, Any],
    change_summary: dict[str, Any] | None = None,
    validation_summary: dict[str, Any] | None = None,
    risk_summary: dict[str, Any] | None = None,
    source_workflow_run_id: UUID | None = None,
) -> CandidateChangeSetCreated:
    change_summary = change_summary or {}
    validation_summary = validation_summary or {}
    risk_summary = risk_summary or {}
    content_hash = canonical_sha256(candidate_manifest)
    idempotency_payload = {
        "project_id": str(project_id),
        "branch_id": str(branch_id),
        "expected_base_revision_id": str(expected_base_revision_id),
        "created_by_principal_id": str(created_by_principal_id),
        "objective": objective,
        "candidate_content_hash": content_hash,
        "change_summary": change_summary,
        "validation_summary": validation_summary,
        "risk_summary": risk_summary,
        "source_workflow_run_id": (
            str(source_workflow_run_id) if source_workflow_run_id else None
        ),
    }
    idempotency_payload_hash = canonical_sha256(idempotency_payload)

    branch = (
        await connection.execute(
            text(
                """
                SELECT head_revision_id, next_revision_number
                FROM project_branches
                WHERE tenant_id=:tenant_id
                  AND project_id=:project_id
                  AND id=:branch_id
                FOR UPDATE
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "branch_id": branch_id,
            },
        )
    ).mappings().one_or_none()
    if branch is None:
        raise KeyError(branch_id)

    existing = (
        await connection.execute(
            text(
                """
                SELECT id, candidate_revision_id, idempotency_payload_hash,
                       (SELECT revision_number FROM project_revisions
                        WHERE id=change_sets.candidate_revision_id) AS revision_number
                FROM change_sets
                WHERE tenant_id=:tenant_id AND idempotency_key=:idempotency_key
                """
            ),
            {"tenant_id": tenant_id, "idempotency_key": idempotency_key},
        )
    ).mappings().one_or_none()
    if existing:
        if existing["idempotency_payload_hash"] != idempotency_payload_hash:
            raise ValueError(
                "change set idempotency key was reused with a different payload"
            )
        return CandidateChangeSetCreated(
            change_set_id=existing["id"],
            candidate_revision_id=existing["candidate_revision_id"],
            revision_number=existing["revision_number"],
            replayed=True,
        )

    if branch["head_revision_id"] != expected_base_revision_id:
        raise StaleBaseRevision(
            "expected_base_revision_id is not the current branch head"
        )
    revision_number = int(branch["next_revision_number"])
    candidate_revision_id = uuid4()
    change_set_id = uuid4()
    await connection.execute(
        text(
            """
            UPDATE project_branches
            SET next_revision_number=:next_revision_number,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=:branch_id
            """
        ),
        {
            "branch_id": branch_id,
            "next_revision_number": revision_number + 1,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO project_revisions (
                id, tenant_id, project_id, branch_id, parent_revision_id,
                revision_number, kind, content_hash, manifest,
                source_workflow_run_id, created_by_principal_id
            )
            VALUES (
                :id, :tenant_id, :project_id, :branch_id, :parent_revision_id,
                :revision_number, 'candidate', :content_hash,
                CAST(:manifest AS jsonb), :source_workflow_id, :creator
            )
            """
        ),
        {
            "id": candidate_revision_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "branch_id": branch_id,
            "parent_revision_id": expected_base_revision_id,
            "revision_number": revision_number,
            "content_hash": content_hash,
            "manifest": _json(candidate_manifest),
            "source_workflow_id": source_workflow_run_id,
            "creator": created_by_principal_id,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO change_sets (
                id, tenant_id, project_id, branch_id, base_revision_id,
                candidate_revision_id, source_workflow_run_id,
                created_by_principal_id, idempotency_key,
                idempotency_payload_hash, objective, change_summary,
                validation_summary, risk_summary
            )
            VALUES (
                :id, :tenant_id, :project_id, :branch_id, :base_revision_id,
                :candidate_revision_id, :source_workflow_id,
                :creator, :idempotency_key, :idempotency_payload_hash,
                :objective, CAST(:change_summary AS jsonb),
                CAST(:validation_summary AS jsonb), CAST(:risk_summary AS jsonb)
            )
            """
        ),
        {
            "id": change_set_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "branch_id": branch_id,
            "base_revision_id": expected_base_revision_id,
            "candidate_revision_id": candidate_revision_id,
            "source_workflow_id": source_workflow_run_id,
            "creator": created_by_principal_id,
            "idempotency_key": idempotency_key,
            "idempotency_payload_hash": idempotency_payload_hash,
            "objective": objective,
            "change_summary": _json(change_summary),
            "validation_summary": _json(validation_summary),
            "risk_summary": _json(risk_summary),
        },
    )
    return CandidateChangeSetCreated(
        change_set_id=change_set_id,
        candidate_revision_id=candidate_revision_id,
        revision_number=revision_number,
    )


async def compare_and_swap_branch_head(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    branch_id: UUID,
    expected_head_revision_id: UUID,
    candidate_revision_id: UUID,
) -> bool:
    candidate_is_child = await connection.scalar(
        text(
            """
            SELECT 1 FROM project_revisions
            WHERE tenant_id=:tenant_id
              AND project_id=:project_id
              AND branch_id=:branch_id
              AND id=:candidate_revision_id
              AND parent_revision_id=:expected_head_revision_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "branch_id": branch_id,
            "candidate_revision_id": candidate_revision_id,
            "expected_head_revision_id": expected_head_revision_id,
        },
    )
    if candidate_is_child is None:
        raise StaleBaseRevision(
            "candidate revision is not a direct child of the expected branch head"
        )
    updated = await connection.scalar(
        text(
            """
            UPDATE project_branches
            SET head_revision_id=:candidate_revision_id,
                updated_at=CURRENT_TIMESTAMP
            WHERE tenant_id=:tenant_id
              AND project_id=:project_id
              AND id=:branch_id
              AND head_revision_id=:expected_head_revision_id
            RETURNING id
            """
        ),
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "branch_id": branch_id,
            "candidate_revision_id": candidate_revision_id,
            "expected_head_revision_id": expected_head_revision_id,
        },
    )
    return updated is not None
