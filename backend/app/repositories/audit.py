"""Append-only audit persistence."""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4, uuid5

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

async def consume_tool_confirmation(connection: AsyncConnection, tenant_id: UUID, actor: UUID,
                                    receipt: UUID, fingerprint: str) -> bool:
    # The accepted record has one deterministic identity per issued receipt. Only
    # one concurrent transaction can consume it, even on different API replicas.
    used = await connection.scalar(text("""
        INSERT INTO audit_records (id, tenant_id, actor_principal_id, action, target_type, target_id, payload)
        SELECT :used, tenant_id, actor_principal_id, 'agent_tool.confirmed', target_type, target_id,
               jsonb_build_object('receipt', id)
        FROM audit_records
        WHERE id=:receipt AND tenant_id=:tenant AND actor_principal_id=:actor
          AND action='agent_tool.consent_required' AND payload->>'fingerprint'=:fingerprint
          AND occurred_at > now() - interval '5 minutes'
        ON CONFLICT (id) DO NOTHING RETURNING id
    """), {"used": uuid5(receipt, "confirmed"), "receipt": receipt, "tenant": tenant_id,
           "actor": actor, "fingerprint": fingerprint})
    return used is not None
