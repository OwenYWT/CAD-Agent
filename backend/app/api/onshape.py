import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.auth import rate_limiter, verify_api_key
from app.config import settings
from app.integrations.onshape import OnshapeAPIError, OnshapeNotConfigured, OnshapeService
from app.storage import auth as auth_store
from app.storage import history
from app.storage.file_ownership import FileOwnershipError, request_belongs_to
from app.models.schemas import (
    OnshapeCreateDocumentRequest,
    OnshapeDocumentResponse,
    OnshapeDocumentsResponse,
    OnshapeLink,
    OnshapePublishRequest,
    OnshapePublishResponse,
)

router = APIRouter(prefix="/api/onshape", tags=["onshape"])
logger = logging.getLogger(__name__)


def _owner_key(api_key: str | None) -> str | None:
    if not api_key:
        return None
    if api_key.startswith("user:"):
        return api_key.split(":", 1)[1]
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return f"api-key:{digest}"


async def _require_document_admin(api_key: str | None) -> None:
    """Shared Onshape-account browsing and writes to existing documents are privileged."""
    if not api_key or not api_key.startswith("user:"):
        return
    user = await auth_store.get_user(api_key.split(":", 1)[1])
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Only administrators can access shared Onshape documents")


async def _require_request_owner(request_id: str, api_key: str | None) -> None:
    try:
        allowed = await request_belongs_to(request_id, api_key)
    except FileOwnershipError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not allowed:
        raise HTTPException(status_code=404, detail="Generated STEP file not found")


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, OnshapeNotConfigured):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, OnshapeAPIError):
        # Never surface an upstream auth failure as a CAD-Agent 401: authFetch would
        # interpret it as an expired application session and log the user out.
        status_code = exc.status_code
        if status_code in {401, 403} or status_code >= 500 or status_code < 400:
            status_code = 502
        return HTTPException(status_code=status_code, detail=exc.to_detail())
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    logger.exception("Unexpected Onshape integration error")
    return HTTPException(status_code=500, detail={"type": type(exc).__name__, "message": str(exc)})


@router.get("/config")
async def onshape_config(
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    return {
        "configured": settings.has_onshape_credentials,
        "default_document_public": settings.onshape_default_document_public,
    }


@router.get("/documents", response_model=OnshapeDocumentsResponse)
async def list_onshape_documents(
    request: Request,
    q: str | None = Query(None, max_length=256),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    await _require_document_admin(api_key)
    try:
        return await OnshapeService().list_documents(q=q, offset=offset, limit=limit)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/documents", response_model=OnshapeDocumentResponse)
async def create_onshape_document(
    req: OnshapeCreateDocumentRequest,
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    await _require_document_admin(api_key)
    try:
        return await OnshapeService().create_document(req)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/publish", response_model=OnshapePublishResponse)
async def publish_to_onshape(
    req: OnshapePublishRequest,
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    await _require_request_owner(req.request_id, api_key)
    if req.document_id:
        await _require_document_admin(api_key)
    try:
        return await OnshapeService().publish_step(req, user_id=_owner_key(api_key))
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/translations/{translation_id}")
async def get_onshape_translation(
    translation_id: str,
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    link = await history.get_onshape_link_by_translation(translation_id, user_id=_owner_key(api_key))
    if not link:
        raise HTTPException(status_code=404, detail="Onshape translation not found")
    try:
        return await OnshapeService().get_translation(translation_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/links/{request_id}", response_model=list[OnshapeLink])
async def get_onshape_links(
    request_id: str,
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    try:
        return await OnshapeService().get_links(request_id, user_id=_owner_key(api_key))
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/links/{request_id}/refresh", response_model=OnshapeLink)
async def refresh_onshape_link(
    request_id: str,
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    try:
        return await OnshapeService().refresh_latest_link(request_id, user_id=_owner_key(api_key))
    except Exception as exc:
        raise _http_error(exc) from exc
