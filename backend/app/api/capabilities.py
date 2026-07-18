"""Read-only capability discovery endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.auth import rate_limiter, verify_api_key
from app.capabilities.models import CapabilityManifest
from app.capabilities.registry import get_capability, list_capabilities


router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])


@router.get("", response_model=list[CapabilityManifest])
async def api_list_capabilities(
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    return list_capabilities()


@router.get("/{capability_id}", response_model=CapabilityManifest)
async def api_get_capability(
    capability_id: str,
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    manifest = get_capability(capability_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Capability '{capability_id}' not found")
    return manifest
