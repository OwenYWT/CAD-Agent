"""Provider-neutral usage metering domain values."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class UsageEntry:
    tenant_id: UUID
    principal_id: UUID
    metric: str
    quantity: int
    unit: str
    billing_dimension: str
    idempotency_key: str
    project_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError("usage quantity must be non-negative")
