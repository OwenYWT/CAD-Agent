"""Read-only capability discovery endpoints."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import settings
from app.temporal_client import worker_runtime_snapshot
from app.api.auth import rate_limiter, verify_api_key
from app.capabilities.models import CapabilityManifest
from app.capabilities.registry import get_capability, list_capabilities


router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])


async def _worker_snapshot():
    if not settings.durable_control_plane_enabled:
        return None
    try:
        return await worker_runtime_snapshot()
    except Exception as exc:
        return exc


@router.get("", response_model=list[CapabilityManifest])
async def api_list_capabilities(
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    return await asyncio.to_thread(list_capabilities, await _worker_snapshot())


@router.get("/{capability_id}", response_model=CapabilityManifest)
async def api_get_capability(
    capability_id: str,
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    manifest = await asyncio.to_thread(get_capability, capability_id, await _worker_snapshot())
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Capability '{capability_id}' not found")
    return manifest
