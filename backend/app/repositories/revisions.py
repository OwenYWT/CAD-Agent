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


async def lock_document_generation(connection, *, tenant_id: UUID, project_id: UUID,
                                   branch_id: UUID):
    """All head writers acquire document, then branch, then candidate locks."""
    return (await connection.execute(text("""
        SELECT head_revision_id, state_version FROM cloud_documents
        WHERE tenant_id=:tenant_id AND project_id=:project_id AND id=:branch_id
        FOR UPDATE
    """), {"tenant_id": tenant_id, "project_id": project_id,
           "branch_id": branch_id})).mappings().one_or_none()


async def lock_candidate_generations(connection, *, tenant_id: UUID, project_id: UUID,
                                     branch_id: UUID, source_workflow_run_id: UUID | None):
    merge = None
    if source_workflow_run_id is not None:
        merge = (await connection.execute(text("""
            SELECT source_document_id, source_revision_id, source_state_version
            FROM document_merges WHERE tenant_id=:tenant AND project_id=:project
              AND document_id=:document AND workflow_run_id=:workflow
        """), {"tenant": tenant_id, "project": project_id, "document": branch_id,
               "workflow": source_workflow_run_id})).mappings().one_or_none()
    ids = {branch_id, merge["source_document_id"]} if merge else {branch_id}
    documents = {}
    for document_id in sorted(ids):
        documents[document_id] = await lock_document_generation(connection,
            tenant_id=tenant_id, project_id=project_id, branch_id=document_id)
    source = documents.get(merge["source_document_id"]) if merge else None
    source_current = not merge or bool(source and (
        source["head_revision_id"] == merge["source_revision_id"]
        and source["state_version"] == merge["source_state_version"]))
    return documents[branch_id], source_current


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

    document, source_current = await lock_candidate_generations(connection, tenant_id=tenant_id,
        project_id=project_id, branch_id=branch_id, source_workflow_run_id=source_workflow_run_id)
    if document is None:
        raise KeyError(branch_id)
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
    if not source_current:
        raise StaleBaseRevision("merge source generation changed; compare both branches again")
    base_state_version = document["state_version"]
    if source_workflow_run_id is not None:
        original = (await connection.execute(text("""
            SELECT document_id, base_revision_id, base_state_version FROM cad_operations
            WHERE tenant_id=:tenant_id AND id=:workflow_id
        """), {"tenant_id": tenant_id, "workflow_id": source_workflow_run_id})).mappings().one_or_none()
        if original is not None and (
            original["document_id"] != branch_id
            or original["base_revision_id"] != expected_base_revision_id
            or original["base_state_version"] != base_state_version
        ):
            raise StaleBaseRevision("operation base generation no longer matches the document")
    # New direct/compatibility candidates capture their actual creation base.
    # This never backfills an existing candidate or changes its request hash.
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
                base_state_version,
                candidate_revision_id, source_workflow_run_id,
                created_by_principal_id, idempotency_key,
                idempotency_payload_hash, objective, change_summary,
                validation_summary, risk_summary
            )
            VALUES (
                :id, :tenant_id, :project_id, :branch_id, :base_revision_id,
                :base_state_version,
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
            "base_state_version": base_state_version,
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
    candidate = (await connection.execute(text("""
        SELECT base_state_version, source_workflow_run_id FROM change_sets
        WHERE tenant_id=:tenant AND project_id=:project AND branch_id=:branch
          AND candidate_revision_id=:candidate AND base_revision_id=:base
    """), {"tenant": tenant_id, "project": project_id, "branch": branch_id,
           "candidate": candidate_revision_id, "base": expected_head_revision_id})).mappings().one_or_none()
    document, source_current = await lock_candidate_generations(connection, tenant_id=tenant_id,
        project_id=project_id, branch_id=branch_id,
        source_workflow_run_id=candidate["source_workflow_run_id"] if candidate else None)
    if document is None or not source_current:
        return False
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
    generation = candidate["base_state_version"] if candidate else None
    if generation is None or generation != document["state_version"]:
        return False
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
