"""Evidence-gated Change Set review, branch commit, and rollback."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.projects import Permission, role_allows
from app.domain.runs import WorkflowStatus
from app.repositories.audit import append_audit_record
from app.repositories.revisions import compare_and_swap_branch_head, lock_candidate_generations
from app.repositories.runs import append_workflow_event
from app.services.run_state import transition_workflow
from app.validation.gate_policy import gate_blocks
from app.object_store import sha256_object


class ChangeSetError(RuntimeError):
    """Base class for review and commit failures."""


class ChangeSetStateConflict(ChangeSetError):
    """The Change Set or branch is no longer in the required state."""


class ValidationRequired(ChangeSetError):
    """Successful validation evidence is required before approval."""


class ArtifactEvidenceRequired(ChangeSetError):
    """Committed immutable artifacts are required before approval."""


def build_agent_change_set_evidence(
    evidence_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project immutable gate evidence into review validation and risks."""
    required = [row for row in evidence_rows if row["mode"] == "required"]
    required_failures = [row for row in evidence_rows
                         if gate_blocks(row["gate"], row["mode"], row["outcome"])]
    incomplete = [row for row in evidence_rows if row["outcome"] != "passed"]
    validation = {
        "status": "failed" if required_failures else "warning" if incomplete else "passed" if required else "unknown",
        "issue_count": len(incomplete),
        "blocking_issue_count": len(required_failures),
        "gates": [
            {
                "gate": row["gate"],
                "mode": row["mode"],
                "outcome": row["outcome"],
                "evidence_id": str(row["id"]),
                "evidence_hash": row["evidence_hash"],
            }
            for row in evidence_rows
        ],
    }
    risks: list[dict[str, Any]] = []
    for row in evidence_rows:
        if row["mode"] != "advisory" or row["outcome"] == "passed":
            continue
        report = dict(row["evidence"] or {})
        risks.append(
            {
                "gate": row["gate"],
                "outcome": row["outcome"],
                "evidence_id": str(row["id"]),
                "issues": list(report.get("issues") or ()),
                "violations": list(report.get("violations") or ()),
            }
        )
    return validation, {
        "status": "attention_required" if risks else "clear",
        "issue_count": len(risks),
        "items": risks,
    }


@dataclass(frozen=True, slots=True)
class ChangeSetOperationResult:
    change_set_id: UUID
    status: str
    replayed: bool = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _locked_change_set(connection, change_set_id: UUID):
    identity = (await connection.execute(text("""
        SELECT tenant_id, project_id, branch_id, source_workflow_run_id FROM change_sets WHERE id=:id
    """), {"id": change_set_id})).mappings().one_or_none()
    if identity is None:
        return None
    document, source_current = await lock_candidate_generations(connection, **dict(identity))
    await connection.execute(text("SELECT id FROM project_branches WHERE id=:id FOR UPDATE"),
                             {"id": identity["branch_id"]})
    row = (
        await connection.execute(
            text(
                """
                SELECT c.*, b.head_revision_id
                FROM change_sets c
                JOIN project_branches b
                  ON b.tenant_id=c.tenant_id
                 AND b.project_id=c.project_id
                 AND b.id=c.branch_id
                WHERE c.id=:change_set_id
                FOR UPDATE OF c
                """
            ),
            {"change_set_id": change_set_id},
        )
    ).mappings().one_or_none()
    return {**dict(row), "current_state_version": document["state_version"] if document else None,
            "merge_source_current": source_current} if row else None


def _require_current_base(row) -> None:
    if not row.get("merge_source_current", True):
        raise ChangeSetStateConflict("stale merge source: 合并来源分支已更新，请重新比较两个分支")
    if row.get("base_state_version") is None:
        raise ChangeSetStateConflict("旧候选缺少可核验的原始版本代次，请基于当前版本重新生成候选")
    if row["base_revision_id"] != row["head_revision_id"] or row["base_state_version"] != row["current_state_version"]:
        raise ChangeSetStateConflict("stale branch head/state version: 候选基线已过期，文档已提交或回退，请基于当前版本重新确认")


async def _require_permission(
    connection,
    row,
    principal_id: UUID,
    permission: Permission,
) -> None:
    from app.services.cloud_documents import _active_workspace_member
    if not await _active_workspace_member(connection, row["tenant_id"], principal_id):
        raise PermissionError("工作区授权已失效")
    role = await connection.scalar(
        text(
            """
            SELECT role FROM project_memberships
            WHERE tenant_id=:tenant_id
              AND project_id=:project_id
              AND principal_id=:principal_id
            """
        ),
        {
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "principal_id": principal_id,
        },
    )
    if not role or not role_allows(role, permission):
        raise PermissionError(
            f"principal lacks {permission.value} permission for this project"
        )


async def _require_evidence(connection, row, *, review_note: str | None = None) -> None:
    validation = dict(row["validation_summary"] or {})
    gates = validation.get("gates") or []
    try:
        blocking = [gate for gate in gates if gate_blocks(
            gate.get("gate"), gate.get("mode"), gate.get("outcome"))]
    except ValueError as exc:
        raise ValidationRequired("候选检查策略或结果无法核验，请重新检查。") from exc
    if blocking:
        raise ValidationRequired("设计一致性或必需检查未通过，不能接受或提交该候选。")
    advisory = [gate for gate in gates if gate.get("mode") == "advisory" and gate.get("outcome") != "passed"]
    valid_gates = bool(gates) and any(gate.get("mode") == "required" for gate in gates)
    if validation.get("status") not in {"passed", "success", "warning"} or (
        not valid_gates and (int(validation.get("issue_count", 0)) != 0 or validation.get("status") == "warning")
    ):
        raise ValidationRequired(
            "successful zero-issue validation evidence is required"
        )
    if advisory and not (review_note or row.get("review_note") or "").strip():
        raise ValidationRequired("仍有建议检查风险或未能判定项，请填写风险审查意见后再接受。")
    evidence = (
        await connection.execute(
            text(
                """
                SELECT count(*) AS artifact_count,
                       bool_and(u.status='committed') AS uploads_committed,
                       bool_and(a.status='succeeded') AS attempts_succeeded
                FROM artifacts f
                JOIN artifact_uploads u ON u.id=f.upload_id
                JOIN execution_attempts a ON a.id=f.attempt_id
                WHERE f.tenant_id=:tenant_id
                  AND f.project_id=:project_id
                  AND f.revision_id=:candidate_revision_id
                  AND NOT EXISTS(SELECT 1 FROM workflow_runs w WHERE w.id=f.workflow_run_id AND w.kind='mcad.scene')
                """
            ),
            {
                "tenant_id": row["tenant_id"],
                "project_id": row["project_id"],
                "candidate_revision_id": row["candidate_revision_id"],
            },
        )
    ).mappings().one()
    if (
        int(evidence["artifact_count"]) < 1
        or evidence["uploads_committed"] is not True
        or evidence["attempts_succeeded"] is not True
    ):
        raise ArtifactEvidenceRequired(
            "at least one committed artifact from a succeeded attempt is required"
        )
    artifacts = (await connection.execute(text("""
        SELECT object_key, sha256, size_bytes FROM artifacts a
        WHERE tenant_id=:tenant AND project_id=:project AND revision_id=:revision
          AND NOT EXISTS(SELECT 1 FROM workflow_runs w WHERE w.id=a.workflow_run_id AND w.kind='mcad.scene')
    """), {"tenant": row["tenant_id"], "project": row["project_id"],
           "revision": row["candidate_revision_id"]})).mappings().all()
    for artifact in artifacts:
        try:
            measured = await sha256_object(artifact["object_key"])
        except Exception as exc:
            raise ArtifactEvidenceRequired("候选文件无法读取，请恢复文件后重试；当前版本未变更。") from exc
        if measured["sha256"] != artifact["sha256"] or measured["size_bytes"] != artifact["size_bytes"]:
            raise ArtifactEvidenceRequired("候选文件完整性校验失败，不能接受或提交。")


async def _record_operation(
    connection,
    row,
    *,
    actor_principal_id: UUID,
    action: str,
    status: str,
    note: str | None,
) -> None:
    await append_audit_record(
        connection,
        tenant_id=row["tenant_id"],
        project_id=row["project_id"],
        actor_principal_id=actor_principal_id,
        action=action,
        target_type="change_set",
        target_id=str(row["id"]),
        payload={
            "status": status,
            "branch_id": str(row["branch_id"]),
            "base_revision_id": str(row["base_revision_id"]),
            "candidate_revision_id": str(row["candidate_revision_id"]),
            "note": note,
        },
    )
    if row["source_workflow_run_id"] is not None:
        await append_workflow_event(
            connection,
            tenant_id=row["tenant_id"],
            workflow_id=row["source_workflow_run_id"],
            event_type=action,
            payload={
                "change_set_id": str(row["id"]),
                "status": status,
                "base_revision_id": str(row["base_revision_id"]),
                "candidate_revision_id": str(row["candidate_revision_id"]),
                "note": note,
            },
        )


async def update_change_set_evidence(
    *,
    tenant_id: UUID,
    principal_id: UUID,
    change_set_id: UUID,
    validation_summary: dict[str, Any],
    risk_summary: dict[str, Any],
    now: datetime | None = None,
) -> ChangeSetOperationResult:
    current_time = now or _utcnow()
    async with tenant_transaction(tenant_id, principal_id) as connection:
        row = await _locked_change_set(connection, change_set_id)
        if row is None:
            raise KeyError(change_set_id)
        await _require_permission(
            connection,
            row,
            principal_id,
            Permission.MODIFY_DESIGN,
        )
        if row["status"] != "pending_review":
            raise ChangeSetStateConflict(
                f"evidence cannot change in {row['status']} state"
            )
        await connection.execute(
            text(
                """
                UPDATE change_sets
                SET validation_summary=CAST(:validation_summary AS jsonb),
                    risk_summary=CAST(:risk_summary AS jsonb),
                    updated_at=:now
                WHERE id=:change_set_id
                """
            ),
            {
                "change_set_id": change_set_id,
                "validation_summary": json.dumps(
                    validation_summary,
                    separators=(",", ":"),
                ),
                "risk_summary": json.dumps(
                    risk_summary,
                    separators=(",", ":"),
                ),
                "now": current_time,
            },
        )
        refreshed = dict(row)
        refreshed["validation_summary"] = validation_summary
        refreshed["risk_summary"] = risk_summary
        await _record_operation(
            connection,
            refreshed,
            actor_principal_id=principal_id,
            action="change_set.evidence_updated",
            status="pending_review",
            note=None,
        )
    return ChangeSetOperationResult(
        change_set_id=change_set_id,
        status="pending_review",
    )


async def accept_change_set(
    *,
    tenant_id: UUID,
    reviewer_principal_id: UUID,
    change_set_id: UUID,
    review_note: str | None = None,
    now: datetime | None = None,
) -> ChangeSetOperationResult:
    current_time = now or _utcnow()
    async with tenant_transaction(tenant_id, reviewer_principal_id) as connection:
        row = await _locked_change_set(connection, change_set_id)
        if row is None:
            raise KeyError(change_set_id)
        await _require_permission(
            connection,
            row,
            reviewer_principal_id,
            Permission.REVIEW_CHANGE,
        )
        if row["status"] in {"accepted", "committed", "rolled_back"}:
            return ChangeSetOperationResult(
                change_set_id=change_set_id,
                status=row["status"],
                replayed=True,
            )
        if row["status"] != "pending_review":
            raise ChangeSetStateConflict(
                f"change set in {row['status']} cannot be accepted"
            )
        _require_current_base(row)
        await _require_evidence(connection, row, review_note=review_note)
        await connection.execute(
            text(
                """
                UPDATE change_sets
                SET status='accepted', reviewed_by_principal_id=:reviewer,
                    review_note=:review_note, reviewed_at=:now,
                    accepted_at=:now, updated_at=:now
                WHERE id=:change_set_id
                """
            ),
            {
                "change_set_id": change_set_id,
                "reviewer": reviewer_principal_id,
                "review_note": review_note,
                "now": current_time,
            },
        )
        await _record_operation(
            connection,
            row,
            actor_principal_id=reviewer_principal_id,
            action="change_set.accepted",
            status="accepted",
            note=review_note,
        )
    return ChangeSetOperationResult(change_set_id=change_set_id, status="accepted")


async def commit_change_set(
    *,
    tenant_id: UUID,
    reviewer_principal_id: UUID,
    change_set_id: UUID,
    now: datetime | None = None,
) -> ChangeSetOperationResult:
    current_time = now or _utcnow()
    async with tenant_transaction(tenant_id, reviewer_principal_id) as connection:
        row = await _locked_change_set(connection, change_set_id)
        if row is None:
            raise KeyError(change_set_id)
        await _require_permission(
            connection,
            row,
            reviewer_principal_id,
            Permission.COMMIT_VERSION,
        )
        if row["status"] == "committed":
            return ChangeSetOperationResult(
                change_set_id=change_set_id,
                status="committed",
                replayed=True,
            )
        if row["status"] != "accepted":
            raise ChangeSetStateConflict(
                f"change set in {row['status']} cannot be committed"
            )
        _require_current_base(row)
        await _require_evidence(connection, row)
        advanced = await compare_and_swap_branch_head(
            connection,
            tenant_id=row["tenant_id"],
            project_id=row["project_id"],
            branch_id=row["branch_id"],
            expected_head_revision_id=row["base_revision_id"],
            candidate_revision_id=row["candidate_revision_id"],
        )
        if not advanced:
            raise ChangeSetStateConflict(
                "stale branch head prevents change set commit"
            )
        await connection.execute(
            text(
                """
                UPDATE change_sets
                SET status='committed', committed_at=:now, updated_at=:now,
                    reviewed_by_principal_id=COALESCE(
                        reviewed_by_principal_id, :reviewer
                    )
                WHERE id=:change_set_id
                """
            ),
            {
                "change_set_id": change_set_id,
                "reviewer": reviewer_principal_id,
                "now": current_time,
            },
        )
        if row["source_workflow_run_id"] is not None:
            workflow_status = await connection.scalar(
                text("SELECT status FROM workflow_runs WHERE id=:id"),
                {"id": row["source_workflow_run_id"]},
            )
            if workflow_status == WorkflowStatus.RUNNING.value:
                await transition_workflow(
                    connection,
                    row["source_workflow_run_id"],
                    expected=WorkflowStatus.RUNNING,
                    target=WorkflowStatus.SUCCEEDED,
                    now=current_time,
                )
            elif workflow_status != WorkflowStatus.SUCCEEDED.value:
                raise ChangeSetStateConflict(
                    f"workflow in {workflow_status} cannot commit a version"
                )
        await _record_operation(
            connection,
            row,
            actor_principal_id=reviewer_principal_id,
            action="change_set.committed",
            status="committed",
            note=row["review_note"],
        )
    return ChangeSetOperationResult(change_set_id=change_set_id, status="committed")


async def request_change_set_modification(
    *,
    tenant_id: UUID,
    reviewer_principal_id: UUID,
    change_set_id: UUID,
    review_note: str,
    now: datetime | None = None,
) -> ChangeSetOperationResult:
    if not review_note.strip():
        raise ValueError("review_note is required")
    return await _terminal_review_action(
        tenant_id=tenant_id,
        reviewer_principal_id=reviewer_principal_id,
        change_set_id=change_set_id,
        target_status="changes_requested",
        action="change_set.changes_requested",
        review_note=review_note,
        now=now,
    )


async def reject_change_set(
    *,
    tenant_id: UUID,
    reviewer_principal_id: UUID,
    change_set_id: UUID,
    review_note: str,
    now: datetime | None = None,
) -> ChangeSetOperationResult:
    if not review_note.strip():
        raise ValueError("review_note is required")
    return await _terminal_review_action(
        tenant_id=tenant_id,
        reviewer_principal_id=reviewer_principal_id,
        change_set_id=change_set_id,
        target_status="rejected",
        action="change_set.rejected",
        review_note=review_note,
        now=now,
    )


async def _terminal_review_action(
    *,
    tenant_id: UUID,
    reviewer_principal_id: UUID,
    change_set_id: UUID,
    target_status: str,
    action: str,
    review_note: str,
    now: datetime | None,
) -> ChangeSetOperationResult:
    current_time = now or _utcnow()
    async with tenant_transaction(tenant_id, reviewer_principal_id) as connection:
        row = await _locked_change_set(connection, change_set_id)
        if row is None:
            raise KeyError(change_set_id)
        await _require_permission(
            connection,
            row,
            reviewer_principal_id,
            Permission.REVIEW_CHANGE,
        )
        if row["status"] == target_status:
            return ChangeSetOperationResult(
                change_set_id=change_set_id,
                status=target_status,
                replayed=True,
            )
        if row["status"] != "pending_review":
            raise ChangeSetStateConflict(
                f"change set in {row['status']} cannot become {target_status}"
            )
        await connection.execute(
            text(
                """
                UPDATE change_sets
                SET status=:status, reviewed_by_principal_id=:reviewer,
                    review_note=:review_note, reviewed_at=:now, updated_at=:now
                WHERE id=:change_set_id
                """
            ),
            {
                "change_set_id": change_set_id,
                "status": target_status,
                "reviewer": reviewer_principal_id,
                "review_note": review_note,
                "now": current_time,
            },
        )
        await _record_operation(
            connection,
            row,
            actor_principal_id=reviewer_principal_id,
            action=action,
            status=target_status,
            note=review_note,
        )
    return ChangeSetOperationResult(
        change_set_id=change_set_id,
        status=target_status,
    )


async def rollback_change_set(
    *,
    tenant_id: UUID,
    reviewer_principal_id: UUID,
    change_set_id: UUID,
    review_note: str,
    now: datetime | None = None,
) -> ChangeSetOperationResult:
    current_time = now or _utcnow()
    async with tenant_transaction(tenant_id, reviewer_principal_id) as connection:
        row = await _locked_change_set(connection, change_set_id)
        if row is None:
            raise KeyError(change_set_id)
        await _require_permission(
            connection,
            row,
            reviewer_principal_id,
            Permission.ROLLBACK_VERSION,
        )
        if row["status"] == "rolled_back":
            return ChangeSetOperationResult(
                change_set_id=change_set_id,
                status="rolled_back",
                replayed=True,
            )
        if row["status"] != "committed":
            raise ChangeSetStateConflict(
                f"change set in {row['status']} cannot be rolled back"
            )
        restored = await connection.scalar(
            text(
                """
                UPDATE project_branches
                SET head_revision_id=:base_revision_id, updated_at=:now
                WHERE tenant_id=:tenant_id
                  AND project_id=:project_id
                  AND id=:branch_id
                  AND head_revision_id=:candidate_revision_id
                RETURNING id
                """
            ),
            {
                "tenant_id": row["tenant_id"],
                "project_id": row["project_id"],
                "branch_id": row["branch_id"],
                "base_revision_id": row["base_revision_id"],
                "candidate_revision_id": row["candidate_revision_id"],
                "now": current_time,
            },
        )
        if restored is None:
            raise ChangeSetStateConflict(
                "branch advanced after this Change Set; rollback is stale"
            )
        await connection.execute(
            text(
                """
                UPDATE change_sets
                SET status='rolled_back', rolled_back_at=:now,
                    review_note=:review_note,
                    reviewed_by_principal_id=:reviewer,
                    updated_at=:now
                WHERE id=:change_set_id
                """
            ),
            {
                "change_set_id": change_set_id,
                "reviewer": reviewer_principal_id,
                "review_note": review_note,
                "now": current_time,
            },
        )
        await _record_operation(
            connection,
            row,
            actor_principal_id=reviewer_principal_id,
            action="change_set.rolled_back",
            status="rolled_back",
            note=review_note,
        )
    return ChangeSetOperationResult(
        change_set_id=change_set_id,
        status="rolled_back",
    )
