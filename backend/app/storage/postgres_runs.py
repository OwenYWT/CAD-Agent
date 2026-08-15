"""PostgreSQL compatibility contract for the process-local workflow manager."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel
from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.identity import (
    IDENTITY_NAMESPACE,
    api_key_principal,
    local_anonymous_principal,
    user_principal,
)
from app.execution.canonical import canonical_sha256
from app.principal_context import bind_principal, current_principal
from app.repositories.identity import ensure_principal
from app.repositories.runs import append_workflow_event


ACTIVE_STATES = {
    "PENDING",
    "RUNNING",
    "INTERRUPTED",
    "RECONCILING",
    "CANCEL_REQUESTED",
}
TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED"}
_TO_DB = {
    "PENDING": "pending",
    "RUNNING": "running",
    "CANCEL_REQUESTED": "cancelling",
    "COMPLETED": "succeeded",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
}
_FROM_DB = {value: key for key, value in _TO_DB.items()}
_FROM_DB.update(
    {
        "planning": "RUNNING",
        "waiting_confirmation": "RUNNING",
        "timed_out": "FAILED",
    }
)


def _context(owner: str | None):
    if owner and owner.startswith("user:"):
        return user_principal(owner.split(":", 1)[1])
    if owner:
        return api_key_principal(owner)
    return local_anonymous_principal()


def _owner_identity(owner: str | None) -> str:
    if not owner:
        return "anonymous"
    return "sha256:" + hashlib.sha256(owner.encode("utf-8")).hexdigest()


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _payload(value: Any) -> dict:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value)


async def initialize() -> None:
    # Alembic owns schema creation; runtime requests never create local tables.
    return None


async def _project_for_scope(connection, context, scope_key: str | None, run_id: UUID):
    if scope_key and scope_key.startswith("ws:"):
        value = scope_key[3:]
        session_id = value.rsplit(":", 1)[0] if ":" in value else ""
        if session_id:
            project_id = await connection.scalar(
                text(
                    """
                    SELECT project_id FROM workspace_sessions
                    WHERE tenant_id=:tenant AND id=:session
                    """
                ),
                {"tenant": context.tenant_id, "session": session_id},
            )
            if project_id:
                return project_id
    project_id = uuid5(
        IDENTITY_NAMESPACE,
        f"compat-workflow-project:{context.tenant_id}:{run_id}",
    )
    await connection.execute(
        text(
            """
            INSERT INTO projects (
                id, tenant_id, name, slug, created_by_principal_id
            ) VALUES (
                :id, :tenant, '异步 CAD 任务', :slug, :principal
            ) ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "id": project_id,
            "tenant": context.tenant_id,
            "slug": f"async-{run_id.hex[:20]}",
            "principal": context.principal_id,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO project_memberships (
                tenant_id, project_id, principal_id, role
            ) VALUES (:tenant, :project, :principal, 'owner')
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "tenant": context.tenant_id,
            "project": project_id,
            "principal": context.principal_id,
        },
    )
    return project_id


async def create_run(
    *,
    kind: str,
    owner: str | None,
    request: dict,
    scope_key: str | None = None,
    run_id: str | None = None,
) -> str:
    context = _context(owner)
    bind_principal(context)
    resolved_id = UUID(run_id) if run_id else uuid4()
    compat = {
        "owner_id": _owner_identity(owner),
        "scope_key": scope_key,
    }
    request_payload = {
        "request": request,
        "compatibility": compat,
    }
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await ensure_principal(connection, context)
        project_id = await _project_for_scope(
            connection,
            context,
            scope_key,
            resolved_id,
        )
        await connection.execute(
            text(
                """
                INSERT INTO workflow_runs (
                    id, tenant_id, project_id, requested_by_principal_id,
                    kind, status, idempotency_key,
                    request_payload_hash, request_payload
                ) VALUES (
                    :id, :tenant, :project, :principal,
                    :kind, 'pending', :idempotency,
                    :payload_hash, CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "id": resolved_id,
                "tenant": context.tenant_id,
                "project": project_id,
                "principal": context.principal_id,
                "kind": kind,
                "idempotency": f"compat:{resolved_id}",
                "payload_hash": canonical_sha256(request_payload),
                "payload": json.dumps(
                    request_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        )
        await append_workflow_event(
            connection,
            tenant_id=context.tenant_id,
            workflow_id=resolved_id,
            event_type="compat.state",
            payload={"state": "PENDING"},
        )
    return str(resolved_id)


async def _transition(
    run_id: str,
    state: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    context = current_principal()
    workflow_id = UUID(run_id)
    status = _TO_DB[state]
    terminal = state in TERMINAL_STATES
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        updated = await connection.execute(
            text(
                """
                UPDATE workflow_runs
                SET status=:status,
                    started_at=CASE WHEN :status='running'
                        THEN COALESCE(started_at, CURRENT_TIMESTAMP)
                        ELSE started_at END,
                    completed_at=CASE WHEN :terminal
                        THEN CURRENT_TIMESTAMP ELSE completed_at END,
                    error_code=:error_code,
                    error_message=:error_message,
                    updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=:tenant AND id=:id
                """
            ),
            {
                "status": status,
                "terminal": terminal,
                "error_code": error_code,
                "error_message": error_message,
                "tenant": context.tenant_id,
                "id": workflow_id,
            },
        )
        if updated.rowcount != 1:
            raise KeyError(run_id)
        await append_workflow_event(
            connection,
            tenant_id=context.tenant_id,
            workflow_id=workflow_id,
            event_type="compat.state",
            payload={
                "state": state,
                "error_code": error_code,
            },
        )


async def mark_running(run_id: str) -> None:
    await _transition(run_id, "RUNNING")


async def append_progress(run_id: str, payload: Any) -> None:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await append_workflow_event(
            connection,
            tenant_id=context.tenant_id,
            workflow_id=UUID(run_id),
            event_type="compat.progress",
            payload={"data": payload},
        )


async def commit_result(
    run_id: str,
    result: BaseModel | dict,
    *,
    state: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict:
    result_data = _payload(result)
    terminal_state = state or "COMPLETED"
    if terminal_state not in TERMINAL_STATES:
        raise ValueError(f"terminal state required, got {terminal_state}")
    error = result_data.get("error") or {}
    code = error_code or error.get("type")
    message = error_message or error.get("message")
    context = current_principal()
    workflow_id = UUID(run_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        updated = await connection.execute(
            text(
                """
                UPDATE workflow_runs SET status=:status,
                    completed_at=CURRENT_TIMESTAMP,
                    error_code=:error_code, error_message=:error_message,
                    updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=:tenant AND id=:id
                """
            ),
            {
                "status": _TO_DB[terminal_state],
                "error_code": code,
                "error_message": message,
                "tenant": context.tenant_id,
                "id": workflow_id,
            },
        )
        if updated.rowcount != 1:
            raise KeyError(run_id)
        await append_workflow_event(
            connection,
            tenant_id=context.tenant_id,
            workflow_id=workflow_id,
            event_type="compat.state",
            payload={"state": terminal_state},
        )
        await append_workflow_event(
            connection,
            tenant_id=context.tenant_id,
            workflow_id=workflow_id,
            event_type="compat.result",
            payload={"result": result_data},
        )
    return result_data


async def request_cancel(
    run_id: str,
    owner: str | None,
) -> bool:
    context = _context(owner)
    bind_principal(context)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        updated = await connection.execute(
            text(
                """
                UPDATE workflow_runs
                SET status='cancelling',
                    cancellation_requested_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=:tenant AND id=:id
                  AND status IN ('pending', 'running')
                  AND request_payload->'compatibility'->>'owner_id'=:owner
                """
            ),
            {
                "tenant": context.tenant_id,
                "id": UUID(run_id),
                "owner": _owner_identity(owner),
            },
        )
        if updated.rowcount != 1:
            return False
        await append_workflow_event(
            connection,
            tenant_id=context.tenant_id,
            workflow_id=UUID(run_id),
            event_type="compat.state",
            payload={"state": "CANCEL_REQUESTED"},
        )
    return True


async def _load_run(run_id: str, context, owner: str | None = None):
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT * FROM workflow_runs
                    WHERE tenant_id=:tenant AND id=:id
                    """
                ),
                {"tenant": context.tenant_id, "id": UUID(run_id)},
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        compatibility = row["request_payload"].get("compatibility") or {}
        if (
            owner is not None
            and compatibility.get("owner_id") != _owner_identity(owner)
        ):
            return None
        result = await connection.scalar(
            text(
                """
                SELECT payload->'result' FROM task_events
                WHERE tenant_id=:tenant AND workflow_run_id=:id
                  AND event_type='compat.result'
                ORDER BY sequence DESC LIMIT 1
                """
            ),
            {"tenant": context.tenant_id, "id": UUID(run_id)},
        )
    state = _FROM_DB.get(row["status"], "FAILED")
    request = row["request_payload"].get("request") or {}
    return {
        "id": str(row["id"]),
        "owner_id": compatibility.get("owner_id"),
        "kind": row["kind"],
        "scope_key": compatibility.get("scope_key"),
        "state": state,
        "request": request,
        "result": result,
        "terminal_result_ref": (
            result.get("request_id") if isinstance(result, dict) else None
        ),
        "terminal_result_committed": result is not None,
        "cancel_requested": row["cancellation_requested_at"] is not None,
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "created_at": _iso(row["created_at"]),
        "started_at": _iso(row["started_at"]),
        "updated_at": _iso(row["updated_at"]),
        "finished_at": _iso(row["completed_at"]),
    }


async def get_run(run_id: str, owner: str | None) -> dict | None:
    context = _context(owner)
    bind_principal(context)
    return await _load_run(run_id, context, owner)


async def get_run_internal(run_id: str) -> dict | None:
    return await _load_run(run_id, current_principal())


async def find_latest_for_scope(
    scope_key: str,
    owner: str | None,
    *,
    active_only: bool = False,
) -> dict | None:
    context = _context(owner)
    bind_principal(context)
    statuses = (
        ["pending", "planning", "running", "waiting_confirmation", "cancelling"]
        if active_only
        else None
    )
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        status_clause = "AND status = ANY(:statuses)" if statuses else ""
        row = (
            await connection.execute(
                text(
                    """
                    SELECT id FROM workflow_runs
                    WHERE tenant_id=:tenant
                      AND request_payload->'compatibility'->>'scope_key'=:scope
                      AND request_payload->'compatibility'->>'owner_id'=:owner
                    """
                    + status_clause
                    + " ORDER BY created_at DESC LIMIT 1"
                ),
                {
                    "tenant": context.tenant_id,
                    "scope": scope_key,
                    "owner": _owner_identity(owner),
                    "statuses": statuses,
                },
            )
        ).mappings().one_or_none()
    return (
        await _load_run(str(row["id"]), context, owner)
        if row is not None
        else None
    )


async def list_events(run_id: str) -> list[dict]:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, workflow_run_id, sequence, event_type,
                           payload, occurred_at
                    FROM task_events
                    WHERE tenant_id=:tenant AND workflow_run_id=:id
                    ORDER BY sequence
                    """
                ),
                {"tenant": context.tenant_id, "id": UUID(run_id)},
            )
        ).mappings().all()
    result = []
    for row in rows:
        payload = row["payload"]
        result.append(
            {
                "id": row["sequence"],
                "workflow_run_id": str(row["workflow_run_id"]),
                "event_type": (
                    "progress"
                    if row["event_type"] == "compat.progress"
                    else "state"
                ),
                "state": payload.get("state"),
                "payload": payload.get("data"),
                "created_at": _iso(row["occurred_at"]),
            }
        )
    return result


async def reconcile_incomplete_runs() -> list[str]:
    # Temporal owns restart recovery in durable mode (Task 8). Runtime startup
    # must not scan across tenants with a privileged bypass role.
    return []
