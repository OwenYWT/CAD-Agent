import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.auth import rate_limiter, verify_api_key
from app.integrations.onshape import OnshapeAPIError, OnshapeNotConfigured, OnshapeService
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


def _user_id_from_api_key(api_key: str | None) -> str | None:
    if api_key and api_key.startswith("user:"):
        return api_key.split(":", 1)[1]
    return None


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, OnshapeNotConfigured):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, OnshapeAPIError):
        status_code = exc.status_code if 400 <= exc.status_code < 600 else 502
        return HTTPException(status_code=status_code, detail=exc.to_detail())
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    logger.exception("Unexpected Onshape integration error")
    return HTTPException(status_code=500, detail={"type": type(exc).__name__, "message": str(exc)})


@router.get("/documents", response_model=OnshapeDocumentsResponse)
async def list_onshape_documents(
    request: Request,
    q: str | None = Query(None, max_length=256),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
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
    try:
        return await OnshapeService().publish_step(req, user_id=_user_id_from_api_key(api_key))
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/translations/{translation_id}")
async def get_onshape_translation(
    translation_id: str,
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
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
        return await OnshapeService().get_links(request_id, user_id=_user_id_from_api_key(api_key))
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
        return await OnshapeService().refresh_latest_link(request_id, user_id=_user_id_from_api_key(api_key))
    except Exception as exc:
        raise _http_error(exc) from exc
