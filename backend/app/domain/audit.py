"""Append-only audit domain values."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AuditRecord:
    tenant_id: UUID
    actor_principal_id: UUID
    action: str
    target_type: str
    target_id: str
    project_id: UUID | None = None
    payload: dict[str, Any] = field(default_factory=dict)
