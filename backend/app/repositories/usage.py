"""Provider-neutral usage meter persistence."""
from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def record_usage(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    principal_id: UUID,
    metric: str,
    quantity: int,
    unit: str,
    billing_dimension: str,
    idempotency_key: str,
    project_id: UUID | None = None,
) -> UUID:
    if quantity < 0:
        raise ValueError("usage quantity must be non-negative")
    usage_id = uuid4()
    stored_id = await connection.scalar(
        text(
            """
            INSERT INTO usage_meter_entries (
                id, tenant_id, project_id, principal_id, metric, quantity,
                unit, billing_dimension, idempotency_key
            )
            VALUES (
                :id, :tenant_id, :project_id, :principal_id, :metric, :quantity,
                :unit, :billing_dimension, :idempotency_key
            )
            ON CONFLICT (tenant_id, idempotency_key)
            DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": usage_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "principal_id": principal_id,
            "metric": metric,
            "quantity": quantity,
            "unit": unit,
            "billing_dimension": billing_dimension,
            "idempotency_key": idempotency_key,
        },
    )
    if stored_id is not None:
        return stored_id
    replayed = (
        await connection.execute(
            text(
                """
                SELECT id, project_id, principal_id, metric, quantity, unit,
                       billing_dimension
                FROM usage_meter_entries
                WHERE tenant_id=:tenant_id AND idempotency_key=:idempotency_key
                """
            ),
            {
                "tenant_id": tenant_id,
                "idempotency_key": idempotency_key,
            },
        )
    ).mappings().one_or_none()
    if replayed is None:
        raise RuntimeError("usage idempotency replay could not be resolved")
    expected = {
        "project_id": project_id,
        "principal_id": principal_id,
        "metric": metric,
        "quantity": quantity,
        "unit": unit,
        "billing_dimension": billing_dimension,
    }
    actual = {field: replayed[field] for field in expected}
    if actual != expected:
        raise ValueError("usage idempotency key was reused with a different payload")
    return replayed["id"]
