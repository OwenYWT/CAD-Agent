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


_STEP_STAGE = {
    "agent_requirements": "planning",
    "agent_decompose": "planning",
    "agent_plan": "planning",
    "agent_model": "modeling",
    "agent_repair": "repair",
    "agent_visual_repair": "repair",
    "agent_geometry_validation": "validation",
    "agent_visual_render": "validation",
    "agent_visual_validation": "validation",
    "agent_dfm_validation": "validation",
}
_STAGE_LABEL = {
    "planning": "需求与方案",
    "modeling": "执行建模",
    "repair": "自动修复",
    "validation": "工程验证",
    "review": "变更审查",
    "complete": "任务完成",
}
_STATUS_PROJECTION = {
    "pending": "queued",
    "ready": "queued",
    "planning": "running",
    "running": "running",
    "waiting_confirmation": "warn",
    "cancelling": "warn",
    "succeeded": "success",
    "reviewable": "success",
    "failed": "failed",
    "timed_out": "failed",
    "cancelled": "skipped",
    "skipped": "skipped",
    "abandoned": "skipped",
}
_GATE_LABEL = {
    "geometry": "几何检查",
    "visual": "视觉检查",
    "dfm": "DFM 检查",
}


def _project_status(value: Any, fallback: str = "running") -> str:
    return _STATUS_PROJECTION.get(str(value or "").lower(), fallback)


def _event_projection(
    event: dict[str, Any],
    *,
    steps_by_id: dict[str, dict[str, Any]],
    attempts_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build a Chinese read projection without mutating persisted events."""
    event_type = str(event["event_type"])
    payload = dict(event.get("payload") or {})
    attempt = attempts_by_id.get(str(payload.get("attempt_id") or ""))
    step_id = payload.get("step_id") or payload.get("step_run_id")
    if step_id is None and attempt is not None:
        step_id = attempt.get("step_run_id")
    step = steps_by_id.get(str(step_id or ""), {})
    step_kind = str(payload.get("kind") or step.get("kind") or "")
    step_key = str(
        payload.get("step_key")
        or payload.get("repair_step_key")
        or step.get("step_key")
        or ""
    )
    stage = _STEP_STAGE.get(step_kind, "planning")
    status = "running"
    label = _STAGE_LABEL[stage]
    message = label

    if event_type == "workflow.created":
        status, message = "queued", "持久任务已创建"
    elif event_type == "workflow.state_changed":
        workflow_status = str(payload.get("status") or "")
        status = _project_status(workflow_status)
        if workflow_status == "waiting_confirmation":
            stage, label, message = "planning", "需求与方案", "计划等待确认"
        elif workflow_status == "succeeded":
            stage, label, message = "complete", "任务完成", "工程任务已完成"
        elif workflow_status in {"failed", "timed_out"}:
            message = str(payload.get("error_message") or "工程任务执行失败")
        elif workflow_status == "cancelled":
            message = "工程任务已取消"
        else:
            message = f"任务状态：{workflow_status or '运行中'}"
    elif event_type == "step.created":
        status, message = "queued", f"{label}已排队"
    elif event_type == "step.state_changed":
        value = str(payload.get("status") or "")
        status = _project_status(value)
        if status == "failed":
            message = str(payload.get("error_message") or f"{label}执行失败")
        elif status == "success":
            message = f"{label}已完成"
        elif status == "running":
            message = f"{label}进行中"
        else:
            message = f"{label}已排队"
    elif event_type == "attempt.created":
        status, message = "queued", f"{label}执行尝试已创建"
    elif event_type in {"attempt.started", "attempt.leased", "attempt.heartbeat"}:
        status, message = "running", f"{label}正在隔离环境中执行"
    elif event_type == "attempt.state_changed":
        status = _project_status(payload.get("status"))
        if status == "failed":
            message = str(payload.get("error_message") or f"{label}执行失败")
        else:
            message = f"{label}状态已更新"
    elif event_type == "attempt.completed":
        status, message = "success", f"{label}计算已完成"
    elif event_type == "agent.requirements.completed":
        stage, label, status, message = "planning", "需求与方案", "success", "工程需求已解析"
    elif event_type == "agent.decomposition.completed":
        stage, label, status, message = "planning", "需求与方案", "success", "建模步骤已拆解"
    elif event_type == "agent.plan.completed":
        stage, label, status, message = "planning", "需求与方案", "success", "执行计划已生成"
    elif event_type == "agent.candidate_build.created":
        stage, label, status, message = "modeling", "执行建模", "running", "候选模型已开始构建"
    elif event_type == "agent.source.generated":
        generator = str(payload.get("generator_kind") or "")
        is_repair = generator.startswith("repair:")
        stage = "repair" if is_repair else "modeling"
        label = _STAGE_LABEL[stage]
        status = "success"
        message = "修复代码已生成" if is_repair else "建模代码已生成"
    elif event_type in {
        "agent.repair.source_generated",
        "agent.visual_repair.source_generated",
    }:
        stage, label, status, message = "repair", "自动修复", "success", "自动修复代码已生成"
    elif event_type == "agent.staging_manifest.accepted":
        stage, label, status, message = "modeling", "执行建模", "success", "候选工程产物已生成"
    elif event_type == "agent.validation_evidence.recorded":
        gate = str(payload.get("gate") or "")
        mode = str(payload.get("mode") or "")
        outcome = str(payload.get("outcome") or "")
        stage, label = "validation", _GATE_LABEL.get(gate, "工程验证")
        status = (
            "success"
            if outcome == "passed"
            else "failed"
            if mode == "required"
            else "warn"
        )
        message = (
            f"{label}已通过"
            if outcome == "passed"
            else f"{label}{'未能判定' if outcome == 'indeterminate' else '发现风险'}"
        )
    elif event_type == "agent.candidate.sealed":
        stage, label, status, message = "review", "变更审查", "success", "候选版本已封装，等待审查"
    elif event_type == "agent.candidate_build.state_changed":
        value = str(payload.get("status") or "")
        stage = "review" if value == "reviewable" else stage
        label = _STAGE_LABEL[stage]
        status = _project_status(value)
        message = "候选版本可以审查" if value == "reviewable" else f"候选模型状态：{value}"

    return {
        "stage": stage,
        "label": label,
        "status": status,
        "message": message,
        "step_key": step_key or None,
        "step_kind": step_kind or None,
        "attempt_number": (
            int(attempt["attempt_number"]) if attempt is not None else None
        ),
        "gate": payload.get("gate"),
        "mode": payload.get("mode"),
        "outcome": payload.get("outcome"),
        "evidence_id": payload.get("evidence_id"),
        "evidence_hash": payload.get("evidence_hash"),
        "risk_count": payload.get("risk_count"),
    }


def project_task_events(
    events: list[dict[str, Any]],
    *,
    step_rows: list[dict[str, Any]],
    attempt_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    steps_by_id = {str(row["id"]): row for row in step_rows}
    attempts_by_id = {str(row["id"]): row for row in attempt_rows}
    return [
        {
            **event,
            "projection": _event_projection(
                event,
                steps_by_id=steps_by_id,
                attempts_by_id=attempts_by_id,
            ),
        }
        for event in events
    ]


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
                           candidate_revision_id, objective, risk_summary,
                           updated_at
                    FROM change_sets
                    WHERE source_workflow_run_id=:workflow_run_id
                    """
                ),
                {"workflow_run_id": workflow_run_id},
            )
        ).mappings().one_or_none()
        candidate = (
            await connection.execute(
                text(
                    """
                    SELECT id, status FROM agent_candidate_builds
                    WHERE workflow_run_id=:workflow_run_id
                    """
                ),
                {"workflow_run_id": workflow_run_id},
            )
        ).mappings().one_or_none()
        plan_payload = await connection.scalar(
            text(
                """
                SELECT payload->'plan' FROM task_events
                WHERE workflow_run_id=:workflow_run_id
                  AND event_type='agent.plan.completed'
                ORDER BY sequence DESC LIMIT 1
                """
            ),
            {"workflow_run_id": workflow_run_id},
        )
        validation_rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, evidence_hash, gate, mode, outcome, evidence
                    FROM agent_validation_evidence
                    WHERE workflow_run_id=:workflow_run_id
                    ORDER BY created_at, id
                    """
                ),
                {"workflow_run_id": workflow_run_id},
            )
        ).mappings().all()
        repair_count = int(
            await connection.scalar(
                text(
                    """
                    SELECT count(*) FROM agent_generated_sources
                    WHERE workflow_run_id=:workflow_run_id
                      AND generator_kind LIKE 'repair:%'
                    """
                ),
                {"workflow_run_id": workflow_run_id},
            )
            or 0
        )

    attempts_by_step: dict[UUID, list[dict[str, Any]]] = {}
    for row in attempt_rows:
        attempts_by_step.setdefault(row["step_run_id"], []).append(dict(row))
    steps = []
    for row in step_rows:
        item = dict(row)
        item["attempts"] = attempts_by_step.get(row["id"], [])
        steps.append(item)
    current_step = next(
        (
            row for row in reversed(steps)
            if row["status"] in {"running", "ready", "pending"}
        ),
        steps[-1] if steps else None,
    )
    current_stage = (
        "review"
        if candidate and candidate["status"] == "reviewable"
        else "complete"
        if workflow["status"] == "succeeded"
        else _STEP_STAGE.get(str(current_step["kind"]), "planning")
        if current_step
        else "planning"
    )
    agent_projection = None
    if candidate is not None or str(workflow["kind"]).startswith("mcad.agent.v2"):
        validations = []
        for row in validation_rows:
            evidence = dict(row["evidence"] or {})
            validations.append(
                {
                    "evidence_id": row["id"],
                    "evidence_hash": row["evidence_hash"],
                    "gate": row["gate"],
                    "mode": row["mode"],
                    "outcome": row["outcome"],
                    "issues": [
                        str(item) for item in evidence.get("issues") or ()
                    ],
                    "violations": [
                        dict(item) for item in evidence.get("violations") or ()
                        if isinstance(item, dict)
                    ],
                }
            )
        agent_projection = {
            "current_stage": current_stage,
            "current_step_key": current_step["step_key"] if current_step else None,
            "current_step_kind": current_step["kind"] if current_step else None,
            "current_status": (
                candidate["status"]
                if candidate and candidate["status"] == "reviewable"
                else current_step["status"]
                if current_step
                else workflow["status"]
            ),
            "candidate_build_id": candidate["id"] if candidate else None,
            "candidate_status": candidate["status"] if candidate else None,
            "repair_count": repair_count,
            "plan": (
                dict(plan_payload)
                if isinstance(plan_payload, dict)
                else None
            ),
            "validations": validations,
            "risk_summary": (
                dict(change_set["risk_summary"] or {}) if change_set else None
            ),
        }
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
        "agent": agent_projection,
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
        step_rows = [
            dict(row)
            for row in (
                await connection.execute(
                    text(
                        "SELECT id, step_key, kind FROM step_runs "
                        "WHERE workflow_run_id=:workflow_run_id"
                    ),
                    {"workflow_run_id": workflow_run_id},
                )
            ).mappings().all()
        ]
        attempt_rows = [
            dict(row)
            for row in (
                await connection.execute(
                    text(
                        "SELECT id, step_run_id, attempt_number "
                        "FROM execution_attempts "
                        "WHERE workflow_run_id=:workflow_run_id"
                    ),
                    {"workflow_run_id": workflow_run_id},
                )
            ).mappings().all()
        ]
    events = project_task_events(
        [dict(row) for row in rows],
        step_rows=step_rows,
        attempt_rows=attempt_rows,
    )
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
        agent_event_rows = []
        agent_step_rows = []
        agent_attempt_rows = []
        if row["source_workflow_run_id"]:
            agent_event_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT id, workflow_run_id, sequence, event_type,
                               payload, occurred_at
                        FROM task_events
                        WHERE workflow_run_id=:workflow_run_id
                          AND (
                            event_type LIKE 'agent.%'
                            OR event_type IN (
                              'step.created', 'step.state_changed',
                              'attempt.created', 'attempt.started',
                              'attempt.completed', 'attempt.state_changed'
                            )
                          )
                        ORDER BY sequence
                        """
                    ),
                    {"workflow_run_id": row["source_workflow_run_id"]},
                )
            ).mappings().all()
            agent_step_rows = (
                await connection.execute(
                    text(
                        "SELECT id, step_key, kind FROM step_runs "
                        "WHERE workflow_run_id=:workflow_run_id"
                    ),
                    {"workflow_run_id": row["source_workflow_run_id"]},
                )
            ).mappings().all()
            agent_attempt_rows = (
                await connection.execute(
                    text(
                        "SELECT id, step_run_id, attempt_number "
                        "FROM execution_attempts "
                        "WHERE workflow_run_id=:workflow_run_id"
                    ),
                    {"workflow_run_id": row["source_workflow_run_id"]},
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
    item["agent_events"] = project_task_events(
        [dict(event) for event in agent_event_rows],
        step_rows=[dict(step) for step in agent_step_rows],
        attempt_rows=[dict(attempt) for attempt in agent_attempt_rows],
    )
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
