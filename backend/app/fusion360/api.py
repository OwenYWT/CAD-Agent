"""Authenticated Web/Agent endpoints for the Fusion CadAdapter."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from app.api.auth import rate_limiter, verify_api_key
from app.config import settings

from .adapter import RemoteFusionAdapter
from .cloud import ApsClient, ApsConfig
from .contract import CAD_ACTION_ADAPTER, ContextRequest, VerifyRequest
from .errors import FusionConnectorError, map_exception
from .token_store import EncryptedTokenStore
from .postgres_token_store import PostgresEncryptedTokenStore

class _StructuredFusionRoute(APIRoute):
    """Keep dependency failures inside the public CadError envelope."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def structured(request: Request):
            try:
                return await original(request)
            except HTTPException as exc:
                if exc.status_code == 401:
                    return _error_response(
                        FusionConnectorError("AUTH_REQUIRED", "Fusion API authentication is required")
                    )
                raise

        return structured


router = APIRouter(
    prefix="/api/cad/fusion360",
    tags=["fusion360"],
    route_class=_StructuredFusionRoute,
)
_aps_client_cache: tuple[tuple[Any, ...], ApsClient] | None = None


def _owner_id(credential: str | None) -> str:
    if credential and credential.startswith("user:"):
        return credential
    if credential:
        return "api-key:" + hashlib.sha256(credential.encode("utf-8")).hexdigest()[:32]
    return "anonymous-local"


def _adapter(credential: str | None) -> RemoteFusionAdapter:
    secret = settings.fusion_runtime_backend_secret
    if not secret and settings.fusion_runtime_backend_secret_file:
        try:
            secret = Path(settings.fusion_runtime_backend_secret_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            secret = ""
    if not secret:
        raise FusionConnectorError("CONNECTOR_OFFLINE", "Fusion connector is not configured")
    return RemoteFusionAdapter(
        base_url=settings.fusion_runtime_url,
        backend_secret=secret,
        owner_id=_owner_id(credential),
        timeout_s=settings.fusion_runtime_timeout_s,
    )


def _aps_client() -> ApsClient:
    global _aps_client_cache
    key = (
        settings.fusion_cloud_enabled, settings.fusion_aps_client_id,
        settings.fusion_aps_client_secret, settings.fusion_aps_redirect_uri,
        settings.fusion_token_encryption_key, settings.fusion_token_db_path,
        settings.durable_control_plane_enabled,
    )
    if _aps_client_cache and _aps_client_cache[0] == key:
        return _aps_client_cache[1]
    store = None
    if settings.fusion_cloud_enabled and settings.fusion_token_encryption_key:
        if settings.durable_control_plane_enabled:
            store = PostgresEncryptedTokenStore(
                settings.fusion_token_encryption_key
            )
        else:
            store = EncryptedTokenStore(
                settings.fusion_token_db_path,
                settings.fusion_token_encryption_key,
            )
    client = ApsClient(
        ApsConfig(
            enabled=settings.fusion_cloud_enabled,
            client_id=settings.fusion_aps_client_id,
            client_secret=settings.fusion_aps_client_secret,
            redirect_uri=settings.fusion_aps_redirect_uri,
        ),
        store,
    )
    _aps_client_cache = (key, client)
    return client


def _error_response(exc: Exception) -> JSONResponse:
    mapped = map_exception(exc)
    return JSONResponse(status_code=mapped.http_status, content={"error": mapped.to_model().model_dump(mode="json")})


async def _gate(request: Request, credential: str | None) -> None:
    try:
        await rate_limiter.check(request, credential)
    except HTTPException as exc:
        if exc.status_code == 429:
            raise FusionConnectorError("RATE_LIMITED", "Fusion API rate limit exceeded") from exc
        raise


@router.get("/status")
async def status(request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        return (await _adapter(credential).refresh_status()).model_dump(mode="json")
    except Exception as exc:
        return _error_response(exc)


@router.get("/capabilities")
async def capabilities(request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        return _adapter(credential).get_capabilities().model_dump(mode="json")
    except Exception as exc:
        return _error_response(exc)


@router.post("/context")
async def context(body: Annotated[Any, Body()], request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        query = ContextRequest.model_validate(body)
        return (await _adapter(credential).get_context(query)).model_dump(mode="json")
    except Exception as exc:
        return _error_response(exc)


@router.post("/actions")
async def actions(body: Annotated[Any, Body()], request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        action = CAD_ACTION_ADAPTER.validate_python(body)
        return (await _adapter(credential).execute(action)).model_dump(mode="json")
    except Exception as exc:
        return _error_response(exc)


@router.post("/verify")
async def verify(body: Annotated[Any, Body()], request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        specification = VerifyRequest.model_validate(body)
        return (await _adapter(credential).verify(specification)).model_dump(mode="json")
    except Exception as exc:
        return _error_response(exc)


@router.get("/requests/{request_id}")
async def request_status(request_id: str, request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        return await _adapter(credential).request_status(request_id)
    except Exception as exc:
        return _error_response(exc)


@router.post("/requests/{request_id}/cancel")
async def cancel(request_id: str, request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        return await _adapter(credential).cancel(request_id)
    except Exception as exc:
        return _error_response(exc)


@router.get("/artifacts/{request_id}/{filename}")
async def artifact(request_id: str, filename: str, request: Request, credential: str | None = Depends(verify_api_key)):
    client: httpx.AsyncClient | None = None
    try:
        await _gate(request, credential)
        adapter = _adapter(credential)
        client = httpx.AsyncClient(base_url=adapter.base_url, timeout=adapter.timeout_s)
        upstream = client.build_request(
            "GET",
            f"/v1/backend/artifacts/{request_id}/{quote(filename, safe='')}",
            headers={
                "Authorization": f"Bearer {adapter.backend_secret}",
                "X-Owner-ID": adapter.owner_id,
            },
        )
        response = await client.send(upstream, stream=True)
        if response.is_error:
            await response.aclose()
            await client.aclose()
            return _error_response(FusionConnectorError("ARTIFACT_INVALID", "Fusion artifact was not found"))

        async def close_upstream() -> None:
            await response.aclose()
            await client.aclose()

        return StreamingResponse(
            response.aiter_bytes(),
            media_type=response.headers.get("content-type", "application/octet-stream"),
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}"},
            background=BackgroundTask(close_upstream),
        )
    except Exception as exc:
        if client is not None:
            await client.aclose()
        return _error_response(exc)


@router.get("/cloud/status")
async def cloud_status(request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        return await _aps_client().status(_owner_id(credential))
    except Exception as exc:
        return _error_response(exc)


@router.post("/cloud/oauth/start")
async def cloud_oauth_start(request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        return await _aps_client().start_oauth_async(_owner_id(credential))
    except Exception as exc:
        return _error_response(exc)


@router.get("/cloud/oauth/callback")
async def cloud_oauth_callback(state: str, code: str):
    try:
        await _aps_client().exchange_code(state, code)
        return {"status": "connected"}
    except Exception as exc:
        return _error_response(exc)


@router.delete("/cloud/oauth/token")
async def cloud_delete_token(request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        await _aps_client().delete_token(_owner_id(credential))
        return {"status": "deleted"}
    except Exception as exc:
        return _error_response(exc)


@router.get("/cloud/hubs")
async def cloud_hubs(request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        return await _aps_client().hubs(_owner_id(credential))
    except Exception as exc:
        return _error_response(exc)


@router.get("/cloud/hubs/{hub_id}/projects")
async def cloud_projects(hub_id: str, request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        return await _aps_client().projects(_owner_id(credential), hub_id)
    except Exception as exc:
        return _error_response(exc)


@router.get("/cloud/projects/{project_id}/top-folders")
async def cloud_top_folders(project_id: str, hub_id: str, request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        return await _aps_client().top_folders(_owner_id(credential), hub_id, project_id)
    except Exception as exc:
        return _error_response(exc)


@router.get("/cloud/projects/{project_id}/folders/{folder_id}/contents")
async def cloud_folder_contents(project_id: str, folder_id: str, request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        return await _aps_client().folder_contents(_owner_id(credential), project_id, folder_id)
    except Exception as exc:
        return _error_response(exc)


@router.get("/cloud/projects/{project_id}/items/{item_id}/versions")
async def cloud_item_versions(project_id: str, item_id: str, request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        return await _aps_client().item_versions(_owner_id(credential), project_id, item_id)
    except Exception as exc:
        return _error_response(exc)


@router.get("/cloud/projects/{project_id}/versions/{version_id}")
async def cloud_version(project_id: str, version_id: str, request: Request, credential: str | None = Depends(verify_api_key)):
    try:
        await _gate(request, credential)
        return await _aps_client().version(_owner_id(credential), project_id, version_id)
    except Exception as exc:
        return _error_response(exc)
