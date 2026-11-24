"""Tenant and principal persistence."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import tenant_transaction
from app.domain.identity import (
    PrincipalContext,
    PrincipalKind,
    TenantKind,
    api_key_principal,
    local_anonymous_principal,
    quarantine_principal,
    user_principal,
)


def _tenant_status(context: PrincipalContext) -> str:
    return "quarantined" if context.quarantined else "active"


def _principal_status(context: PrincipalContext) -> str:
    return "quarantined" if context.quarantined else "active"


def _membership_role(context: PrincipalContext) -> str:
    if context.kind is PrincipalKind.SERVICE:
        return "service"
    return "owner"


def _tenant_name(context: PrincipalContext, display_name: str | None) -> str:
    return {
        TenantKind.PERSONAL: display_name or "个人工作区",
        TenantKind.ORGANIZATION: display_name or "团队工作区",
        TenantKind.SERVICE: display_name or "API 服务工作区",
        TenantKind.LOCAL_DEVELOPMENT: "本地开发工作区",
        TenantKind.QUARANTINE: "待认领数据隔离区",
    }[context.tenant_kind]


async def ensure_principal(
    connection: AsyncConnection,
    context: PrincipalContext,
    *,
    display_name: str | None = None,
) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO tenants (id, kind, name, status)
            VALUES (:id, :kind, :name, :status)
            ON CONFLICT (id) DO UPDATE
            SET name=EXCLUDED.name, updated_at=CURRENT_TIMESTAMP
            """
        ),
        {
            "id": context.tenant_id,
            "kind": context.tenant_kind.value,
            "name": _tenant_name(context, display_name),
            "status": _tenant_status(context),
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO principals (
                id, tenant_id, kind, external_subject, display_name,
                api_key_fingerprint, status
            )
            VALUES (
                :id, :tenant_id, :kind, :external_subject, :display_name,
                :api_key_fingerprint, :status
            )
            ON CONFLICT (id) DO UPDATE
            SET display_name=COALESCE(EXCLUDED.display_name, principals.display_name),
                updated_at=CURRENT_TIMESTAMP
            """
        ),
        {
            "id": context.principal_id,
            "tenant_id": context.tenant_id,
            "kind": context.kind.value,
            "external_subject": context.external_subject,
            "display_name": display_name,
            "api_key_fingerprint": context.api_key_fingerprint,
            "status": _principal_status(context),
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO tenant_memberships (tenant_id, principal_id, role)
            VALUES (:tenant_id, :principal_id, :role)
            ON CONFLICT (tenant_id, principal_id) DO NOTHING
            """
        ),
        {
            "tenant_id": context.tenant_id,
            "principal_id": context.principal_id,
            "role": _membership_role(context),
        },
    )


async def reconcile_principal(
    context: PrincipalContext,
    *,
    display_name: str | None = None,
) -> PrincipalContext:
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        current = await connection.scalar(text("""
            SELECT 1 FROM tenant_memberships m
            JOIN tenants t ON t.id=m.tenant_id
            JOIN principals p ON p.id=m.principal_id AND p.tenant_id=t.id
            WHERE m.tenant_id=:tenant AND m.principal_id=:principal AND t.name=:tenant_name
              AND (CAST(:display_name AS text) IS NULL OR p.display_name=:display_name)
        """), {"tenant": context.tenant_id, "principal": context.principal_id,
               "tenant_name": _tenant_name(context, display_name), "display_name": display_name})
        if not current:
            await ensure_principal(connection, context, display_name=display_name)
    return context


async def reconcile_authenticated_user(user: dict) -> PrincipalContext:
    context = user_principal(str(user["id"]))
    display_name = user.get("phone") or str(user["id"])
    return await reconcile_principal(context, display_name=display_name)


async def reconcile_api_key(api_key: str) -> PrincipalContext:
    return await reconcile_principal(api_key_principal(api_key))


async def reconcile_local_anonymous() -> PrincipalContext:
    return await reconcile_principal(local_anonymous_principal())


async def reconcile_quarantine(source_reference: str) -> PrincipalContext:
    return await reconcile_principal(quarantine_principal(source_reference))


async def count_principals(connection: AsyncConnection, tenant_id: UUID) -> int:
    return int(
        await connection.scalar(
            text("SELECT count(*) FROM principals WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        )
        or 0
    )
