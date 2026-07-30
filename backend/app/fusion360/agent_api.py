"""Authenticated HTTPS API for the direct Fusion Cloud Agent workflow."""

from __future__ import annotations

import hashlib
import inspect
import os
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError

from app.api.auth import rate_limiter, verify_api_key
from app.config import settings

from .agent_contract import (
    MAX_AGENT_ARTIFACT_BYTES,
    AgentArtifactUploadClaim,
    AgentExecutionReport,
    AgentHeartbeatRequest,
    AgentProtocolCapabilities,
    AgentTurnRequest,
)
from .agent_planner import AgentPlanner, AgentPlanningError
from .agent_store import AgentStore, AgentStoreError
from .postgres_agent_store import PostgresAgentStore
from .artifacts import _has_signature
from .contract import CAD_ACTION_ADAPTER
from .policy import action_intent_hash, sha256_canonical

class _StructuredAgentRoute(APIRoute):
    """Normalize dependency failures before they can become FastAPI detail bodies."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def structured(request: Request):
            try:
                return await original(request)
            except HTTPException as exc:
                if exc.status_code == 401:
                    return _error_response(
                        AgentPlanningError(
                            "Fusion Cloud Agent authentication is required",
                            code="AGENT_AUTH_REQUIRED",
                            http_status=401,
                        )
                    )
                if isinstance(exc.detail, dict) and isinstance(exc.detail.get("error"), dict):
                    return JSONResponse(status_code=exc.status_code, content=exc.detail)
                return _error_response(
                    AgentPlanningError(
                        "Fusion Cloud Agent request dependency failed",
                        code="AGENT_DEPENDENCY_UNAVAILABLE",
                        http_status=exc.status_code,
                    )
                )

        return structured


router = APIRouter(
    prefix="/api/cad/fusion360/agent",
    tags=["fusion360-agent"],
    route_class=_StructuredAgentRoute,
)

_planner_cache: AgentPlanner | None = None
_store_cache: tuple[tuple[str, str, int, bool], Any] | None = None


def _owner_id(credential: str | None) -> str:
    if credential and credential.startswith("user:"):
        return credential
    if credential:
        digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()[:32]
        return f"api-key:{digest}"
    return "anonymous-local"


def get_agent_planner() -> AgentPlanner:
    global _planner_cache
    if _planner_cache is None:
        try:
            _planner_cache = AgentPlanner.from_settings(settings)
        except AgentPlanningError as exc:
            # Dependency construction happens before the route body, so map it here
            # instead of relying on endpoint exception handling.
            raise HTTPException(
                status_code=exc.http_status,
                detail={"error": {"code": exc.code, "message": exc.message, "retryable": False}},
            ) from exc
    return _planner_cache


def get_agent_store() -> Any:
    global _store_cache
    key = (
        settings.fusion_agent_db_path,
        settings.fusion_agent_artifact_dir,
        settings.fusion_agent_artifact_max_bytes,
        settings.durable_control_plane_enabled,
    )
    if _store_cache is not None and _store_cache[0] != key:
        _store_cache[1].close()
        _store_cache = None
    if _store_cache is None:
        if settings.durable_control_plane_enabled:
            store = PostgresAgentStore(
                artifact_root=key[1],
                max_artifact_bytes=min(key[2], MAX_AGENT_ARTIFACT_BYTES),
            )
        else:
            store = AgentStore(
                key[0],
                artifact_root=key[1],
                max_artifact_bytes=min(key[2], MAX_AGENT_ARTIFACT_BYTES),
            )
        _store_cache = (key, store)
    return _store_cache[1]


async def _gate(request: Request, credential: str | None) -> None:
    try:
        await rate_limiter.check(request, credential)
    except HTTPException as exc:
        if exc.status_code == 429:
            raise AgentStoreError(
                "Fusion Cloud Agent rate limit exceeded", code="AGENT_RATE_LIMITED", http_status=429
            ) from exc
        raise


def _error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, (AgentPlanningError, AgentStoreError)):
        status = exc.http_status
        code = exc.code
        message = exc.message
        retryable = status in {429, 503, 504}
    elif isinstance(exc, ValidationError):
        status = 422
        code = "AGENT_REQUEST_INVALID"
        message = "Fusion Cloud Agent request validation failed"
        retryable = False
    else:
        status = 500
        code = "AGENT_INTERNAL_ERROR"
        message = "Fusion Cloud Agent request failed"
        retryable = False
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "retryable": retryable}},
    )


def _reject_private_result_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.startswith("_"):
                raise AgentPlanningError("execution result contains a private/internal field")
            _reject_private_result_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_private_result_keys(child)


async def _store_call(method, *args, **kwargs):
    value = method(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


@router.get("/capabilities", response_model=AgentProtocolCapabilities)
async def capabilities(
    request: Request,
    credential: str | None = Depends(verify_api_key),
):
    try:
        await _gate(request, credential)
        return AgentProtocolCapabilities(max_artifact_bytes=min(settings.fusion_agent_artifact_max_bytes, MAX_AGENT_ARTIFACT_BYTES))
    except Exception as exc:
        return _error_response(exc)


@router.post("/heartbeat")
async def heartbeat(
    body: Annotated[Any, Body()],
    request: Request,
    credential: str | None = Depends(verify_api_key),
    store: AgentStore = Depends(get_agent_store),
):
    try:
        await _gate(request, credential)
        heartbeat_request = AgentHeartbeatRequest.model_validate(body)
        receipt = await _store_call(
            store.record_heartbeat,
            _owner_id(credential),
            heartbeat_request,
        )
        return receipt.model_dump(mode="json")
    except Exception as exc:
        return _error_response(exc)


@router.get("/connectors/{connector_instance_id}/status")
async def connector_status(
    connector_instance_id: uuid.UUID,
    request: Request,
    credential: str | None = Depends(verify_api_key),
    store: AgentStore = Depends(get_agent_store),
):
    try:
        await _gate(request, credential)
        status = await _store_call(
            store.connector_status,
            _owner_id(credential),
            connector_instance_id,
        )
        return status.model_dump(mode="json")
    except Exception as exc:
        return _error_response(exc)


@router.post("/plan")
async def plan(
    body: Annotated[Any, Body()],
    request: Request,
    credential: str | None = Depends(verify_api_key),
    planner: AgentPlanner = Depends(get_agent_planner),
    store: AgentStore = Depends(get_agent_store),
):
    try:
        await _gate(request, credential)
        turn = AgentTurnRequest.model_validate(body)
        owner_id = _owner_id(credential)
        turn_hash = sha256_canonical(turn.model_dump(mode="json"))
        existing = await _store_call(
            store.find_plan,
            owner_id,
            turn.request_id,
            turn_hash,
        )
        if existing is not None:
            return existing.model_dump(mode="json")
        response = await planner.plan(turn)
        intent_hash = action_intent_hash(response.action) if response.action else None
        stored, _ = await _store_call(
            store.save_plan,
            owner_id=owner_id,
            turn_hash=turn_hash,
            response=response,
            action_intent_hash=intent_hash,
            export_upload_consent=turn.export_artifact_upload_consent,
            f3d_upload_authorized=turn.f3d_upload_authorized,
        )
        return stored.model_dump(mode="json")
    except Exception as exc:
        return _error_response(exc)


@router.post("/results")
async def results(
    body: Annotated[Any, Body()],
    request: Request,
    credential: str | None = Depends(verify_api_key),
    store: AgentStore = Depends(get_agent_store),
):
    try:
        await _gate(request, credential)
        if not isinstance(body, dict):
            raise AgentPlanningError("execution report must be a JSON object")
        _reject_private_result_keys(body.get("result"))
        report = AgentExecutionReport.model_validate(body)
        owner_id = _owner_id(credential)
        plan_record = await _store_call(
            store.plan_record,
            owner_id,
            report.request_id,
        )
        if (
            plan_record["proposal_id"] != str(report.proposal_id)
            or plan_record["connector_instance_id"] != str(report.connector_instance_id)
            or plan_record["context_fingerprint"] != report.context_fingerprint
        ):
            raise AgentStoreError(
                "execution report does not match its request-scoped plan",
                code="AGENT_REPORT_BINDING_MISMATCH",
                http_status=409,
            )
        if plan_record["status"] != "proposed" or not plan_record["action"]:
            raise AgentStoreError(
                "execution reports are only accepted for proposed actions",
                code="AGENT_REPORT_BINDING_MISMATCH",
                http_status=409,
            )
        planned_action = CAD_ACTION_ADAPTER.validate_python(plan_record["action"])
        if (
            action_intent_hash(planned_action) != report.action_intent_hash
            or report.action_intent_hash != plan_record["action_intent_hash"]
            or report.result.action != planned_action.action
        ):
            raise AgentStoreError(
                "execution report action does not match the proposed action intent",
                code="AGENT_REPORT_BINDING_MISMATCH",
                http_status=409,
            )
        report_hash = sha256_canonical(report.model_dump(mode="json"))
        receipt = await _store_call(
            store.save_report,
            owner_id=owner_id,
            report_hash=report_hash,
            report=report,
        )
        return receipt.model_dump(mode="json")
    except Exception as exc:
        return _error_response(exc)


@router.put("/artifacts/{request_id}/{filename}")
async def artifact_upload(
    request_id: uuid.UUID,
    filename: str,
    request: Request,
    x_artifact_sha256: Annotated[str, Header(alias="X-Artifact-SHA256")],
    x_artifact_size: Annotated[int, Header(alias="X-Artifact-Size")],
    credential: str | None = Depends(verify_api_key),
    store: AgentStore = Depends(get_agent_store),
):
    temporary_path: Path | None = None
    try:
        await _gate(request, credential)
        claim = AgentArtifactUploadClaim(
            request_id=request_id,
            filename=filename,
            size_bytes=x_artifact_size,
            sha256=x_artifact_sha256,
        )
        if claim.size_bytes > store.max_artifact_bytes:
            raise AgentStoreError(
                "artifact exceeds the configured upload limit",
                code="AGENT_ARTIFACT_TOO_LARGE",
                http_status=413,
            )
        owner_id = _owner_id(credential)
        plan_record = await _store_call(
            store.authorize_artifact,
            owner_id,
            claim,
        )
        temporary_path = store.staging_path(owner_id, request_id, filename)
        descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest = hashlib.sha256()
        total = 0
        head = bytearray()
        try:
            with os.fdopen(descriptor, "wb") as handle:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > claim.size_bytes or total > store.max_artifact_bytes:
                        raise AgentStoreError(
                            "artifact body exceeds its declared/configured size",
                            code="AGENT_ARTIFACT_TOO_LARGE",
                            http_status=413,
                        )
                    if len(head) < 512:
                        head.extend(chunk[: 512 - len(head)])
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        if total != claim.size_bytes or digest.hexdigest() != claim.sha256:
            raise AgentStoreError(
                "artifact size or SHA-256 does not match its upload claim",
                code="AGENT_ARTIFACT_INVALID",
                http_status=422,
            )
        if not _has_signature(plan_record["export_format"], bytes(head), total):
            raise AgentStoreError(
                "artifact content does not match the approved export format",
                code="AGENT_ARTIFACT_INVALID",
                http_status=422,
            )
        receipt = await _store_call(
            store.commit_artifact,
            owner_id=owner_id,
            claim=claim,
            media_type=request.headers.get("content-type", "application/octet-stream"),
            temporary_path=temporary_path,
        )
        temporary_path = None
        return receipt.model_copy(update={
            "download_url": f"/api/cad/fusion360/agent/artifacts/{request_id}/{filename}"
        }).model_dump(mode="json")
    except Exception as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        return _error_response(exc)


@router.get("/artifacts/{request_id}/{filename}")
async def artifact_download(
    request_id: uuid.UUID,
    filename: str,
    request: Request,
    credential: str | None = Depends(verify_api_key),
    store: AgentStore = Depends(get_agent_store),
):
    try:
        await _gate(request, credential)
        # Reuse the same strict basename grammar as upload before touching the
        # owner-confined store.
        AgentArtifactUploadClaim(
            request_id=request_id,
            filename=filename,
            size_bytes=1,
            sha256="0" * 64,
        )
        owner_id = _owner_id(credential)
        if hasattr(store, "artifact_bytes"):
            record, payload = await _store_call(
                store.artifact_bytes,
                owner_id,
                request_id,
                filename,
            )
            return Response(
                content=payload,
                media_type=record["media_type"],
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{filename}"'
                    ),
                    "ETag": f'"{record["sha256"]}"',
                },
            )
        record = await _store_call(
            store.artifact_record,
            owner_id,
            request_id,
            filename,
        )
        return FileResponse(
            record["stored_path"],
            media_type=record["media_type"],
            filename=filename,
        )
    except Exception as exc:
        return _error_response(exc)
