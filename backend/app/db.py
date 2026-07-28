"""PostgreSQL connection boundary for the durable control plane."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.config import settings


_engine: AsyncEngine | None = None
DatabaseRole = Literal["runtime", "worker"]
_DATABASE_ROLES: dict[DatabaseRole, str] = {
    "runtime": "cad_agent_runtime",
    "worker": "cad_agent_worker",
}


def get_database_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
        )
    return _engine


@asynccontextmanager
async def tenant_transaction(
    tenant_id: UUID,
    principal_id: UUID | None = None,
    *,
    role: DatabaseRole = "runtime",
) -> AsyncIterator[AsyncConnection]:
    """Open one tenant-scoped transaction with pool-safe PostgreSQL context.

    `SET LOCAL` is deliberately transaction-bound: a pooled connection cannot
    retain one customer's tenant or principal when it is reused by another.
    The local development migration owner assumes the same non-owner roles that
    production connections receive directly.
    """
    role_name = _DATABASE_ROLES[role]
    async with get_database_engine().begin() as connection:
        await connection.execute(text(f"SET LOCAL ROLE {role_name}"))
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        await connection.execute(
            text("SELECT set_config('app.principal_id', :principal_id, true)"),
            {"principal_id": str(principal_id) if principal_id else ""},
        )
        yield connection


async def database_readiness() -> dict:
    started = time.perf_counter()

    async def _probe() -> None:
        async with get_database_engine().connect() as connection:
            value = await connection.scalar(text("SELECT 1"))
            if value != 1:
                raise RuntimeError("PostgreSQL readiness query returned an invalid value")

    await asyncio.wait_for(
        _probe(),
        timeout=settings.dependency_readiness_timeout_s,
    )
    return {
        "status": "ready",
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


async def database_round_trip() -> dict:
    """Perform a real transactional SQL write/read used by infrastructure QA."""
    marker = "cad-agent-durable-probe"
    async with get_database_engine().begin() as connection:
        await connection.execute(
            text(
                "CREATE TEMPORARY TABLE IF NOT EXISTS infrastructure_probe "
                "(value TEXT NOT NULL) ON COMMIT DROP"
            )
        )
        await connection.execute(
            text("INSERT INTO infrastructure_probe (value) VALUES (:value)"),
            {"value": marker},
        )
        value = await connection.scalar(
            text("SELECT value FROM infrastructure_probe LIMIT 1")
        )
    if value != marker:
        raise RuntimeError("PostgreSQL round-trip returned unexpected data")
    return {"status": "success", "value": value}


async def close_database() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
