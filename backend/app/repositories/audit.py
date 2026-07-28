"""Append-only audit persistence."""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


_SENSITIVE_AUDIT_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "session_token",
    "token",
}


def _redact_audit_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if key.strip().lower() in _SENSITIVE_AUDIT_KEYS
                else _redact_audit_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_audit_payload(item) for item in value]
    return value


async def append_audit_record(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    actor_principal_id: UUID,
    action: str,
    target_type: str,
    target_id: str,
    payload: dict[str, Any],
    project_id: UUID | None = None,
) -> UUID:
    audit_id = uuid4()
    await connection.execute(
        text(
            """
            INSERT INTO audit_records (
                id, tenant_id, project_id, actor_principal_id, action,
                target_type, target_id, payload
            )
            VALUES (
                :id, :tenant_id, :project_id, :actor_principal_id, :action,
                :target_type, :target_id, CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "id": audit_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "actor_principal_id": actor_principal_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "payload": json.dumps(
                _redact_audit_payload(payload),
                separators=(",", ":"),
            ),
        },
    )
    return audit_id
