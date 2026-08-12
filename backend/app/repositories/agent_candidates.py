"""Transactional persistence for pre-product durable Agent candidates."""
from __future__ import annotations

import json
import hashlib
import hmac
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.artifacts import (
    CandidateSealCreated,
    GeneratedSourceRecorded,
    StagingManifestAccepted,
    ValidationEvidenceRecorded,
)
from app.domain.revisions import (
    AgentCandidateBuildCompleted,
    AgentCandidateBuildCreated,
    CandidateBuildStatus,
)
from app.execution.canonical import canonical_sha256
from app.repositories.runs import append_workflow_event


class CandidateBuildConflict(RuntimeError):
    """An immutable candidate input or lifecycle transition conflicted."""


class StaleExecutionAttempt(RuntimeError):
    """A staging result was submitted by a stale or unsuccessful attempt."""


class ValidationEvidenceConflict(RuntimeError):
    """Validation evidence does not belong to the selected candidate output."""


async def record_generated_source(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    candidate_build_id: UUID,
    workflow_id: UUID,
    step_id: UUID,
    source_code: str,
    generator_kind: str,
    provider: str,
    model: str,
    request_hash: str,
    response_hash: str,
    provider_response_id: str | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    predecessor_source_id: UUID | None = None,
    input_source_ids: tuple[UUID, ...] = (),
) -> GeneratedSourceRecorded:
    if not source_code.strip():
        raise ValueError("generated source cannot be empty")
    source_hash = hashlib.sha256(source_code.encode("utf-8")).hexdigest()
    for label, value in (
        ("source_hash", source_hash),
        ("request_hash", request_hash),
        ("response_hash", response_hash),
    ):
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError(f"{label} must be a lowercase SHA-256")
    candidate = (
        await connection.execute(
            text(
                """
                SELECT status, workflow_run_id FROM agent_candidate_builds
                WHERE tenant_id=:tenant_id AND id=:candidate_build_id FOR UPDATE
                """
            ),
            {"tenant_id": tenant_id, "candidate_build_id": candidate_build_id},
        )
    ).mappings().one_or_none()
    if candidate is None:
        raise KeyError(candidate_build_id)
    if candidate["status"] != CandidateBuildStatus.BUILDING.value:
        raise CandidateBuildConflict("source generation requires a building candidate")
    if candidate["workflow_run_id"] != workflow_id:
        raise CandidateBuildConflict("source belongs to another workflow")
    if len(input_source_ids) != len(set(input_source_ids)):
        raise ValueError("generated source inputs cannot contain duplicates")
    referenced_source_ids = tuple(
        dict.fromkeys(
            [
                *(input_source_ids or ()),
                *([predecessor_source_id] if predecessor_source_id else []),
            ]
        )
    )
    if referenced_source_ids:
        predecessor = (
            await connection.execute(
                text(
                    """
                    SELECT id, candidate_build_id FROM agent_generated_sources
                    WHERE tenant_id=:tenant_id AND id = ANY(:source_ids)
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "source_ids": list(referenced_source_ids),
                },
            )
        ).mappings().all()
        if {row["id"] for row in predecessor} != set(referenced_source_ids):
            raise KeyError("one or more generated source inputs do not exist")
        if any(
            row["candidate_build_id"] != candidate_build_id
            for row in predecessor
        ):
            raise CandidateBuildConflict(
                "generated source input belongs to another candidate"
            )
    existing = (
        await connection.execute(
            text(
                """
                SELECT id, source_hash, generator_kind, provider, model,
                       provider_response_id, request_hash, response_hash,
                       finish_reason, usage, predecessor_source_id
                FROM agent_generated_sources
                WHERE tenant_id=:tenant_id AND step_run_id=:step_id
                """
            ),
            {"tenant_id": tenant_id, "step_id": step_id},
        )
    ).mappings().one_or_none()
    immutable = {
        "source_hash": source_hash,
        "generator_kind": generator_kind,
        "provider": provider,
        "model": model,
        "provider_response_id": provider_response_id,
        "request_hash": request_hash,
        "response_hash": response_hash,
        "finish_reason": finish_reason,
        "usage": usage or {},
        "predecessor_source_id": predecessor_source_id,
    }
    if existing is not None:
        stored_inputs = tuple(
            (
                await connection.execute(
                    text(
                        """
                        SELECT input_source_id
                        FROM agent_generated_source_inputs
                        WHERE tenant_id=:tenant_id AND source_id=:source_id
                        ORDER BY ordinal
                        """
                    ),
                    {"tenant_id": tenant_id, "source_id": existing["id"]},
                )
            ).scalars()
        )
        if any(existing[key] != value for key, value in immutable.items()):
            raise CandidateBuildConflict(
                "modeling step already recorded different generated source"
            )
        if stored_inputs != input_source_ids:
            raise CandidateBuildConflict(
                "modeling step already recorded different source inputs"
            )
        return GeneratedSourceRecorded(
            source_id=existing["id"], source_hash=source_hash, replayed=True
        )
    source_id = uuid4()
    await connection.execute(
        text(
            """
            INSERT INTO agent_generated_sources (
                id, tenant_id, candidate_build_id, workflow_run_id,
                step_run_id, predecessor_source_id, source_hash, source_code,
                generator_kind, provider, model, provider_response_id,
                request_hash, response_hash, finish_reason, usage
            ) VALUES (
                :id, :tenant_id, :candidate_build_id, :workflow_id,
                :step_id, :predecessor_source_id, :source_hash, :source_code,
                :generator_kind, :provider, :model, :provider_response_id,
                :request_hash, :response_hash, :finish_reason,
                CAST(:usage AS jsonb)
            )
            """
        ),
        {
            "id": source_id,
            "tenant_id": tenant_id,
            "candidate_build_id": candidate_build_id,
            "workflow_id": workflow_id,
            "step_id": step_id,
            "predecessor_source_id": predecessor_source_id,
            "source_hash": source_hash,
            "source_code": source_code,
            "generator_kind": generator_kind,
            "provider": provider,
            "model": model,
            "provider_response_id": provider_response_id,
            "request_hash": request_hash,
            "response_hash": response_hash,
            "finish_reason": finish_reason,
            "usage": _json(usage or {}),
        },
    )
    for ordinal, input_source_id in enumerate(input_source_ids):
        await connection.execute(
            text(
                """
                INSERT INTO agent_generated_source_inputs (
                    tenant_id, source_id, input_source_id, ordinal
                ) VALUES (
                    :tenant_id, :source_id, :input_source_id, :ordinal
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "source_id": source_id,
                "input_source_id": input_source_id,
                "ordinal": ordinal,
            },
        )
    await append_workflow_event(
        connection,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        event_type="agent.source.generated",
        payload={
            "candidate_build_id": str(candidate_build_id),
            "source_id": str(source_id),
            "step_run_id": str(step_id),
            "source_hash": source_hash,
            "generator_kind": generator_kind,
            "provider": provider,
            "model": model,
            "provider_response_id": provider_response_id,
            "request_hash": request_hash,
            "response_hash": response_hash,
            "input_source_ids": [str(item) for item in input_source_ids],
        },
    )
    return GeneratedSourceRecorded(source_id=source_id, source_hash=source_hash)


async def get_generated_source_for_step(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    workflow_id: UUID,
    step_key: str,
) -> dict[str, Any] | None:
    row = (
        await connection.execute(
            text(
                """
                SELECT g.*, s.step_key
                FROM agent_generated_sources g
                JOIN step_runs s ON s.id=g.step_run_id
                WHERE g.tenant_id=:tenant_id
                  AND g.workflow_run_id=:workflow_id
                  AND s.step_key=:step_key
                """
            ),
            {
                "tenant_id": tenant_id,
                "workflow_id": workflow_id,
                "step_key": step_key,
            },
        )
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


async def get_staging_manifest_for_step(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    candidate_build_id: UUID,
    workflow_id: UUID,
    step_key: str,
) -> dict[str, Any] | None:
    row = (
        await connection.execute(
            text(
                """
                SELECT m.*, a.result_payload, s.step_key
                FROM agent_staging_manifests m
                JOIN step_runs s ON s.id=m.step_run_id
                JOIN execution_attempts a ON a.id=m.execution_attempt_id
                WHERE m.tenant_id=:tenant_id
                  AND m.candidate_build_id=:candidate_build_id
                  AND m.workflow_run_id=:workflow_id
                  AND s.step_key=:step_key
                ORDER BY a.attempt_number DESC
                LIMIT 1
                """
            ),
            {
                "tenant_id": tenant_id,
                "candidate_build_id": candidate_build_id,
                "workflow_id": workflow_id,
                "step_key": step_key,
            },
        )
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


async def create_agent_candidate_build(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    branch_id: UUID,
    base_revision_id: UUID,
    workflow_id: UUID,
    created_by_principal_id: UUID,
    plan: dict[str, Any],
) -> AgentCandidateBuildCreated:
    plan_hash = canonical_sha256(plan)
    existing = (
        await connection.execute(
            text(
                """
                SELECT id, status, project_id, branch_id, base_revision_id,
                       created_by_principal_id, plan_hash
                FROM agent_candidate_builds
                WHERE tenant_id=:tenant_id AND workflow_run_id=:workflow_id
                FOR UPDATE
                """
            ),
            {"tenant_id": tenant_id, "workflow_id": workflow_id},
        )
    ).mappings().one_or_none()
    expected = {
        "project_id": project_id,
        "branch_id": branch_id,
        "base_revision_id": base_revision_id,
        "created_by_principal_id": created_by_principal_id,
        "plan_hash": plan_hash,
    }
    if existing is not None:
        if any(existing[key] != value for key, value in expected.items()):
            raise CandidateBuildConflict(
                "candidate build replay used different immutable inputs"
            )
        return AgentCandidateBuildCreated(
            candidate_build_id=existing["id"],
            status=CandidateBuildStatus(existing["status"]),
            replayed=True,
        )

    branch_head = await connection.scalar(
        text(
            """
            SELECT head_revision_id FROM project_branches
            WHERE tenant_id=:tenant_id AND project_id=:project_id
              AND id=:branch_id FOR UPDATE
            """
        ),
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "branch_id": branch_id,
        },
    )
    if branch_head is None:
        raise KeyError(branch_id)
    if branch_head != base_revision_id:
        raise CandidateBuildConflict(
            "expected base revision is not the current branch head"
        )
    candidate_build_id = uuid4()
    await connection.execute(
        text(
            """
            INSERT INTO agent_candidate_builds (
                id, tenant_id, project_id, branch_id, base_revision_id,
                workflow_run_id, created_by_principal_id, plan_hash
            ) VALUES (
                :id, :tenant_id, :project_id, :branch_id, :base_revision_id,
                :workflow_id, :creator, :plan_hash
            )
            """
        ),
        {
            "id": candidate_build_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "branch_id": branch_id,
            "base_revision_id": base_revision_id,
            "workflow_id": workflow_id,
            "creator": created_by_principal_id,
            "plan_hash": plan_hash,
        },
    )
    await append_workflow_event(
        connection,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        event_type="agent.candidate_build.created",
        payload={
            "candidate_build_id": str(candidate_build_id),
            "plan_hash": plan_hash,
            "status": CandidateBuildStatus.BUILDING.value,
        },
    )
    return AgentCandidateBuildCreated(
        candidate_build_id=candidate_build_id,
        status=CandidateBuildStatus.BUILDING,
    )


async def transition_agent_candidate_build(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    candidate_build_id: UUID,
    expected: CandidateBuildStatus,
    target: CandidateBuildStatus,
    failure_code: str | None = None,
    failure_message: str | None = None,
) -> AgentCandidateBuildCompleted:
    allowed = {
        CandidateBuildStatus.BUILDING: {
            CandidateBuildStatus.REVIEWABLE,
            CandidateBuildStatus.FAILED,
            CandidateBuildStatus.CANCELLED,
            CandidateBuildStatus.ABANDONED,
        },
        CandidateBuildStatus.REVIEWABLE: set(),
        CandidateBuildStatus.FAILED: set(),
        CandidateBuildStatus.CANCELLED: set(),
        CandidateBuildStatus.ABANDONED: set(),
    }
    if target not in allowed[expected]:
        raise CandidateBuildConflict(
            f"candidate transition {expected.value} -> {target.value} is invalid"
        )
    row = (
        await connection.execute(
            text(
                """
                SELECT status, workflow_run_id, candidate_revision_id,
                       change_set_id
                FROM agent_candidate_builds
                WHERE tenant_id=:tenant_id AND id=:id FOR UPDATE
                """
            ),
            {"tenant_id": tenant_id, "id": candidate_build_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise KeyError(candidate_build_id)
    status = CandidateBuildStatus(row["status"])
    if status == target:
        return AgentCandidateBuildCompleted(
            candidate_build_id=candidate_build_id,
            status=target,
            replayed=True,
        )
    if status != expected:
        raise CandidateBuildConflict(
            f"candidate expected {expected.value}, found {status.value}"
        )
    if target is CandidateBuildStatus.REVIEWABLE and (
        row["candidate_revision_id"] is None or row["change_set_id"] is None
    ):
        raise CandidateBuildConflict(
            "candidate cannot become reviewable before seal product links exist"
        )
    terminal_failure = target is CandidateBuildStatus.FAILED
    await connection.execute(
        text(
            """
            UPDATE agent_candidate_builds
            SET status=:status, failure_code=:failure_code,
                failure_message=:failure_message,
                completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE id=:id
            """
        ),
        {
            "id": candidate_build_id,
            "status": target.value,
            "failure_code": failure_code if terminal_failure else None,
            "failure_message": failure_message if terminal_failure else None,
        },
    )
    await append_workflow_event(
        connection,
        tenant_id=tenant_id,
        workflow_id=row["workflow_run_id"],
        event_type="agent.candidate_build.state_changed",
        payload={
            "candidate_build_id": str(candidate_build_id),
            "previous_status": expected.value,
            "status": target.value,
            "failure_code": failure_code if terminal_failure else None,
        },
    )
    return AgentCandidateBuildCompleted(
        candidate_build_id=candidate_build_id,
        status=target,
    )


async def accept_staging_manifest(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    candidate_build_id: UUID,
    workflow_id: UUID,
    step_id: UUID,
    attempt_id: UUID,
    lease_generation: int,
    manifest: dict[str, Any],
    lease_token: str,
    supersedes_id: UUID | None = None,
) -> StagingManifestAccepted:
    if lease_generation < 1:
        raise ValueError("lease_generation must be positive")
    manifest_hash = canonical_sha256(manifest)
    attempt = (
        await connection.execute(
            text(
                """
                SELECT status, workflow_run_id, step_run_id, lease_generation,
                       lease_token_hash
                FROM execution_attempts
                WHERE tenant_id=:tenant_id AND id=:attempt_id FOR UPDATE
                """
            ),
            {"tenant_id": tenant_id, "attempt_id": attempt_id},
        )
    ).mappings().one_or_none()
    if attempt is None:
        raise KeyError(attempt_id)
    if (
        attempt["status"] != "succeeded"
        or attempt["workflow_run_id"] != workflow_id
        or attempt["step_run_id"] != step_id
        or int(attempt["lease_generation"]) != lease_generation
        or not hmac.compare_digest(
            attempt["lease_token_hash"] or "",
            hashlib.sha256(lease_token.encode("utf-8")).hexdigest(),
        )
    ):
        raise StaleExecutionAttempt(
            "only the current succeeded attempt lease may submit a manifest"
        )
    candidate = (
        await connection.execute(
            text(
                """
                SELECT status, workflow_run_id FROM agent_candidate_builds
                WHERE tenant_id=:tenant_id AND id=:candidate_build_id FOR UPDATE
                """
            ),
            {"tenant_id": tenant_id, "candidate_build_id": candidate_build_id},
        )
    ).mappings().one_or_none()
    if candidate is None:
        raise KeyError(candidate_build_id)
    if candidate["status"] != CandidateBuildStatus.BUILDING.value:
        raise CandidateBuildConflict(
            "staging manifests require a building candidate"
        )
    if candidate["workflow_run_id"] != workflow_id:
        raise CandidateBuildConflict("manifest belongs to another workflow")
    if supersedes_id is not None:
        superseded = (
            await connection.execute(
                text(
                    """
                    SELECT candidate_build_id, step_run_id
                    FROM agent_staging_manifests
                    WHERE tenant_id=:tenant_id AND id=:id
                    """
                ),
                {"tenant_id": tenant_id, "id": supersedes_id},
            )
        ).mappings().one_or_none()
        if superseded is None:
            raise KeyError(supersedes_id)
        if (
            superseded["candidate_build_id"] != candidate_build_id
            or superseded["step_run_id"] != step_id
        ):
            raise CandidateBuildConflict(
                "superseded manifest must belong to the same candidate step"
            )
    existing = (
        await connection.execute(
            text(
                """
                SELECT id, manifest_hash, manifest FROM agent_staging_manifests
                WHERE tenant_id=:tenant_id
                  AND execution_attempt_id=:attempt_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "attempt_id": attempt_id,
            },
        )
    ).mappings().one_or_none()
    if existing is not None:
        if (
            existing["manifest_hash"] != manifest_hash
            or dict(existing["manifest"]) != manifest
        ):
            raise CandidateBuildConflict(
                "execution attempt already accepted a different manifest"
            )
        return StagingManifestAccepted(
            staging_manifest_id=existing["id"],
            manifest_hash=manifest_hash,
            replayed=True,
        )
    manifest_id = uuid4()
    await connection.execute(
        text(
            """
            INSERT INTO agent_staging_manifests (
                id, tenant_id, candidate_build_id, workflow_run_id,
                step_run_id, execution_attempt_id, lease_generation,
                manifest_hash, manifest, supersedes_id
            ) VALUES (
                :id, :tenant_id, :candidate_build_id, :workflow_id,
                :step_id, :attempt_id, :lease_generation,
                :manifest_hash, CAST(:manifest AS jsonb), :supersedes_id
            )
            """
        ),
        {
            "id": manifest_id,
            "tenant_id": tenant_id,
            "candidate_build_id": candidate_build_id,
            "workflow_id": workflow_id,
            "step_id": step_id,
            "attempt_id": attempt_id,
            "lease_generation": lease_generation,
            "manifest_hash": manifest_hash,
            "manifest": _json(manifest),
            "supersedes_id": supersedes_id,
        },
    )
    await append_workflow_event(
        connection,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        event_type="agent.staging_manifest.accepted",
        payload={
            "candidate_build_id": str(candidate_build_id),
            "staging_manifest_id": str(manifest_id),
            "step_run_id": str(step_id),
            "execution_attempt_id": str(attempt_id),
            "manifest_hash": manifest_hash,
            "supersedes_id": str(supersedes_id) if supersedes_id else None,
        },
    )
    return StagingManifestAccepted(
        staging_manifest_id=manifest_id,
        manifest_hash=manifest_hash,
    )


async def record_validation_evidence(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    candidate_build_id: UUID,
    workflow_id: UUID,
    step_id: UUID,
    staging_manifest_id: UUID,
    gate: str,
    mode: str,
    outcome: str,
    evidence: dict[str, Any],
    attempt_id: UUID | None = None,
) -> ValidationEvidenceRecorded:
    if gate not in {"artifact_integrity", "geometry", "visual", "dfm"}:
        raise ValueError("unsupported validation gate")
    if mode not in {"required", "advisory"}:
        raise ValueError("validation mode must be required or advisory")
    if outcome not in {"passed", "failed", "indeterminate"}:
        raise ValueError("unsupported validation outcome")
    manifest = (
        await connection.execute(
            text(
                """
                SELECT candidate_build_id, workflow_run_id, step_run_id
                FROM agent_staging_manifests
                WHERE tenant_id=:tenant_id AND id=:manifest_id
                """
            ),
            {"tenant_id": tenant_id, "manifest_id": staging_manifest_id},
        )
    ).mappings().one_or_none()
    if manifest is None:
        raise KeyError(staging_manifest_id)
    if (
        manifest["candidate_build_id"] != candidate_build_id
        or manifest["workflow_run_id"] != workflow_id
    ):
        raise ValidationEvidenceConflict(
            "evidence references a manifest from another candidate"
        )
    evidence_hash = canonical_sha256(evidence)
    existing = (
        await connection.execute(
            text(
                """
                SELECT id, evidence FROM agent_validation_evidence
                WHERE tenant_id=:tenant_id
                  AND candidate_build_id=:candidate_build_id
                  AND staging_manifest_id=:staging_manifest_id
                  AND gate=:gate AND evidence_hash=:evidence_hash
                """
            ),
            {
                "tenant_id": tenant_id,
                "candidate_build_id": candidate_build_id,
                "staging_manifest_id": staging_manifest_id,
                "gate": gate,
                "evidence_hash": evidence_hash,
            },
        )
    ).mappings().one_or_none()
    if existing is not None:
        if dict(existing["evidence"]) != evidence:
            raise ValidationEvidenceConflict("evidence hash collision detected")
        return ValidationEvidenceRecorded(
            evidence_id=existing["id"],
            evidence_hash=evidence_hash,
            replayed=True,
        )
    evidence_id = uuid4()
    await connection.execute(
        text(
            """
            INSERT INTO agent_validation_evidence (
                id, tenant_id, candidate_build_id, workflow_run_id,
                step_run_id, execution_attempt_id, staging_manifest_id,
                gate, mode, outcome, evidence_hash, evidence
            ) VALUES (
                :id, :tenant_id, :candidate_build_id, :workflow_id,
                :step_id, :attempt_id, :staging_manifest_id,
                :gate, :mode, :outcome, :evidence_hash,
                CAST(:evidence AS jsonb)
            )
            """
        ),
        {
            "id": evidence_id,
            "tenant_id": tenant_id,
            "candidate_build_id": candidate_build_id,
            "workflow_id": workflow_id,
            "step_id": step_id,
            "attempt_id": attempt_id,
            "staging_manifest_id": staging_manifest_id,
            "gate": gate,
            "mode": mode,
            "outcome": outcome,
            "evidence_hash": evidence_hash,
            "evidence": _json(evidence),
        },
    )
    await append_workflow_event(
        connection,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        event_type="agent.validation_evidence.recorded",
        payload={
            "candidate_build_id": str(candidate_build_id),
            "evidence_id": str(evidence_id),
            "staging_manifest_id": str(staging_manifest_id),
            "gate": gate,
            "mode": mode,
            "outcome": outcome,
            "evidence_hash": evidence_hash,
        },
    )
    return ValidationEvidenceRecorded(
        evidence_id=evidence_id,
        evidence_hash=evidence_hash,
    )


async def create_candidate_seal(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    candidate_build_id: UUID,
    seal_key: str,
    selection: dict[str, Any],
) -> CandidateSealCreated:
    if not seal_key.strip():
        raise ValueError("seal_key is required")
    selection_hash = canonical_sha256(selection)
    candidate_status = await connection.scalar(
        text(
            "SELECT status FROM agent_candidate_builds "
            "WHERE tenant_id=:tenant_id AND id=:id FOR UPDATE"
        ),
        {"tenant_id": tenant_id, "id": candidate_build_id},
    )
    if candidate_status is None:
        raise KeyError(candidate_build_id)
    if candidate_status != CandidateBuildStatus.BUILDING.value:
        raise CandidateBuildConflict("seal requires a building candidate")
    existing = (
        await connection.execute(
            text(
                """
                SELECT id, candidate_build_id, seal_key, selection_hash
                FROM agent_candidate_seals
                WHERE tenant_id=:tenant_id
                  AND (seal_key=:seal_key OR candidate_build_id=:candidate_build_id)
                FOR UPDATE
                """
            ),
            {
                "tenant_id": tenant_id,
                "seal_key": seal_key,
                "candidate_build_id": candidate_build_id,
            },
        )
    ).mappings().one_or_none()
    if existing is not None:
        if (
            existing["candidate_build_id"] != candidate_build_id
            or existing["seal_key"] != seal_key
            or existing["selection_hash"] != selection_hash
        ):
            raise CandidateBuildConflict(
                "seal key replay used a different candidate selection"
            )
        return CandidateSealCreated(
            seal_id=existing["id"],
            selection_hash=selection_hash,
            replayed=True,
        )
    seal_id = uuid4()
    await connection.execute(
        text(
            """
            INSERT INTO agent_candidate_seals (
                id, tenant_id, candidate_build_id, seal_key, selection_hash
            ) VALUES (
                :id, :tenant_id, :candidate_build_id, :seal_key,
                :selection_hash
            )
            """
        ),
        {
            "id": seal_id,
            "tenant_id": tenant_id,
            "candidate_build_id": candidate_build_id,
            "seal_key": seal_key,
            "selection_hash": selection_hash,
        },
    )
    return CandidateSealCreated(
        seal_id=seal_id,
        selection_hash=selection_hash,
    )
