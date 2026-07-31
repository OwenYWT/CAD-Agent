"""Authorized immutable project branch, revision, and artifact reads."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import get_durable_principal
from app.domain.identity import PrincipalContext
from app.services.event_relay import (
    get_revision_detail,
    list_branch_revisions,
    list_project_branches,
)


router = APIRouter(prefix="/api/projects", tags=["revisions"])


def _read_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="未找到项目版本")
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail="无权访问该项目")
    raise exc


@router.get("/{project_id}/branches")
async def project_branches(
    project_id: UUID,
    principal: PrincipalContext = Depends(get_durable_principal),
):
    try:
        return await list_project_branches(principal, project_id)
    except Exception as exc:
        raise _read_error(exc) from exc


@router.get("/{project_id}/branches/{branch_id}/revisions")
async def branch_revisions(
    project_id: UUID,
    branch_id: UUID,
    principal: PrincipalContext = Depends(get_durable_principal),
):
    try:
        return await list_branch_revisions(principal, project_id, branch_id)
    except Exception as exc:
        raise _read_error(exc) from exc


@router.get("/{project_id}/revisions/{revision_id}")
async def revision_detail(
    project_id: UUID,
    revision_id: UUID,
    principal: PrincipalContext = Depends(get_durable_principal),
):
    try:
        return await get_revision_detail(principal, project_id, revision_id)
    except Exception as exc:
        raise _read_error(exc) from exc
