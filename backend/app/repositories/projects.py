"""Project persistence and membership policy checks."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.projects import Permission, ProjectRole, role_allows


async def create_project(
    connection: AsyncConnection,
    *,
    project_id: UUID,
    tenant_id: UUID,
    creator_principal_id: UUID,
    name: str,
    slug: str,
) -> UUID:
    await connection.execute(
        text(
            """
            INSERT INTO projects (
                id, tenant_id, name, slug, created_by_principal_id
            )
            VALUES (
                :id, :tenant_id, :name, :slug, :creator_principal_id
            )
            """
        ),
        {
            "id": project_id,
            "tenant_id": tenant_id,
            "name": name,
            "slug": slug,
            "creator_principal_id": creator_principal_id,
        },
    )
    await add_project_member(
        connection,
        tenant_id=tenant_id,
        project_id=project_id,
        principal_id=creator_principal_id,
        role=ProjectRole.OWNER,
    )
    return project_id


async def add_project_member(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID,
    role: ProjectRole,
) -> None:
    await connection.execute(
        text(
            """
            INSERT INTO project_memberships (
                tenant_id, project_id, principal_id, role
            )
            VALUES (:tenant_id, :project_id, :principal_id, :role)
            ON CONFLICT (tenant_id, project_id, principal_id)
            DO UPDATE SET role=EXCLUDED.role
            """
        ),
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "principal_id": principal_id,
            "role": role.value,
        },
    )


async def principal_has_permission(
    connection: AsyncConnection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID,
    permission: Permission,
) -> bool:
    role = await connection.scalar(
        text(
            """
            SELECT role FROM project_memberships
            WHERE tenant_id=:tenant_id
              AND project_id=:project_id
              AND principal_id=:principal_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "principal_id": principal_id,
        },
    )
    return bool(role and role_allows(str(role), permission))
