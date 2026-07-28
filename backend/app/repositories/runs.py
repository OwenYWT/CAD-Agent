"""Low-level PostgreSQL persistence for durable runs and their event outbox."""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.runs import EventAppended


_SENSITIVE_EVENT_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "session_token",
    "token",
}


def _redact_event_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if key.strip().lower() in _SENSITIVE_EVENT_KEYS
                else _redact_event_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_event_payload(item) for item in value]
    return value


async def append_workflow_event(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    workflow_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    topic: str = "workflow.events",
) -> EventAppended:
    """Atomically allocate a per-workflow sequence and create its outbox row."""
    sequence = await connection.scalar(
        text(
            """
            UPDATE workflow_runs
            SET last_event_sequence=last_event_sequence + 1,
                updated_at=CURRENT_TIMESTAMP
            WHERE tenant_id=:tenant_id AND id=:workflow_id
            RETURNING last_event_sequence
            """
        ),
        {"tenant_id": tenant_id, "workflow_id": workflow_id},
    )
    if sequence is None:
        raise KeyError(f"workflow {workflow_id} not found")

    event_id = uuid4()
    outbox_id = uuid4()
    safe_payload = _redact_event_payload(payload)
    payload_json = json.dumps(safe_payload, separators=(",", ":"), ensure_ascii=False)
    await connection.execute(
        text(
            """
            INSERT INTO task_events (
                id, tenant_id, workflow_run_id, sequence, event_type, payload
            )
            VALUES (
                :id, :tenant_id, :workflow_id, :sequence, :event_type,
                CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "id": event_id,
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "sequence": sequence,
            "event_type": event_type,
            "payload": payload_json,
        },
    )
    outbox_payload = {
        "event_id": str(event_id),
        "tenant_id": str(tenant_id),
        "workflow_id": str(workflow_id),
        "sequence": int(sequence),
        "event_type": event_type,
        "payload": safe_payload,
    }
    await connection.execute(
        text(
            """
            INSERT INTO outbox_messages (
                id, tenant_id, workflow_run_id, event_id, topic, payload
            )
            VALUES (
                :id, :tenant_id, :workflow_id, :event_id, :topic,
                CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "id": outbox_id,
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "event_id": event_id,
            "topic": topic,
            "payload": json.dumps(
                outbox_payload,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        },
    )
    return EventAppended(
        event_id=event_id,
        outbox_id=outbox_id,
        sequence=int(sequence),
    )
