"""Authenticated upload, execution, and artifact endpoints for CAD Skills.

The public API never accepts a command line or an absolute path. Inputs are
uploaded into an owner-scoped workspace and the runtime constructs allowlisted
commands from typed action parameters.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from app.api.files import FileUploadForm, VerifiedArtifactResponse, read_upload_form
from pydantic import BaseModel, Field

from app.api.auth import rate_limiter, verify_api_key
from app.capabilities.artifacts import ArtifactPathError, ArtifactStore, sha256_file
from app.capabilities.registry import has_capability
from app.services.capability_actions import owner_root, perform_action, uses_native_worker
from app.temporal_client import run_worker_command, WorkerCommandConflict, TemporalWorkerUnavailable
from app.principal_context import current_principal
from temporalio.client import WorkflowFailureError
from temporalio.service import RPCError
from app.config import settings


router = APIRouter(tags=["capability-actions"])

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
_ALLOWED_UPLOAD_SUFFIXES = {
    ".step", ".stp", ".stl", ".obj", ".3mf", ".ply", ".glb", ".gltf",
    ".dxf", ".svg", ".png", ".gif", ".gcode", ".urdf", ".srdf", ".sdf",
    ".py", ".json", ".js", ".mjs",
}


class CapabilityActionRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = Field(None, min_length=1, max_length=128)


class CapabilityActionResponse(BaseModel):
    status: Literal["succeeded", "blocked", "failed", "dry_run"]
    capability: str
    action: str
    request_id: str
    data: Any = None
    files: list[dict[str, Any]] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    command_preview: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    error: str | None = None


def _owner_id(api_key: str | None) -> str:
    # verify_api_key returns user:<id> for login sessions. Hashing also keeps legacy
    # static keys out of directory names and filesystem diagnostics.
    identity = api_key or "anonymous-local-development"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _safe_upload_name(raw_name: str | None) -> str:
    name = Path(raw_name or "artifact.bin").name
    if name != (raw_name or "artifact.bin") or not _SAFE_FILENAME.fullmatch(name):
        raise HTTPException(status_code=400, detail="Filename must use letters, digits, '.', '_' or '-'.")
    lower = name.lower()
    if not any(lower.endswith(suffix) for suffix in _ALLOWED_UPLOAD_SUFFIXES):
        raise HTTPException(status_code=400, detail="File type is not supported by a CAD Skills workflow.")
    return name


@router.post("/api/capability-artifacts", status_code=201, openapi_extra=FileUploadForm.openapi_body())
async def upload_capability_artifact(
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    async with read_upload_form(request, FileUploadForm) as form:
        file = form.file
        filename = _safe_upload_name(file.filename)
        upload_id = uuid.uuid4().hex
        owner = owner_root(_owner_id(api_key))
        target_dir = owner / "uploads" / upload_id
        target_dir.mkdir(parents=True, exist_ok=False)
        target = target_dir / filename
        size = 0
        try:
            with target.open("xb") as handle:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.capability_upload_max_bytes:
                        raise HTTPException(status_code=413, detail="Artifact exceeds the configured upload limit.")
                    handle.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            try:
                target_dir.rmdir()
            except OSError:
                pass
            raise

    digest = await asyncio.to_thread(sha256_file, target)
    if settings.durable_control_plane_enabled:
        from app.storage.capability_artifacts import commit

        await commit(
            scope="uploads",
            request_id=upload_id,
            filename=filename,
            path=target,
            content_type=file.content_type,
        )
    return {
        "id": upload_id,
        "name": filename,
        "input": f"uploads/{upload_id}/{filename}",
        "url": f"/api/capability-artifacts/uploads/{upload_id}/{filename}",
        "size_bytes": size,
        "sha256": digest,
        "media_type": file.content_type or "application/octet-stream",
    }


@router.get("/api/capability-artifacts/{scope}/{request_id}/{filename}")
async def download_capability_artifact(
    scope: Literal["uploads", "runs"],
    request_id: str,
    filename: str,
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    if settings.durable_control_plane_enabled:
        from app.storage.capability_artifacts import get

        metadata = await get(scope, request_id, filename)
        if metadata is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return VerifiedArtifactResponse(metadata, filename=filename)
    owner = owner_root(_owner_id(api_key))
    try:
        if scope == "runs":
            store = ArtifactStore(owner / "runs", workspace_root=owner)
            path = store.output_path(request_id, filename, create_parent=False)
        else:
            store = ArtifactStore(owner / "uploads", workspace_root=owner)
            path = store.output_path(request_id, filename, create_parent=False)
    except ArtifactPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(path, filename=path.name)


@router.post(
    "/api/capability-actions/{capability_id}/{action_id}",
    response_model=CapabilityActionResponse,
)
async def run_capability_action(
    capability_id: str,
    action_id: str,
    payload: CapabilityActionRequest,
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    if not has_capability(capability_id):
        raise HTTPException(status_code=404, detail=f"Capability '{capability_id}' not found")
    owner = _owner_id(api_key)
    if settings.durable_control_plane_enabled and uses_native_worker(capability_id, action_id):
        principal = current_principal()
        try:
            request_id = ArtifactStore.validate_request_id(payload.request_id or uuid.uuid4().hex)
        except ArtifactPathError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        command = {"owner": owner, "tenant_id": str(principal.tenant_id),
                   "principal_id": str(principal.principal_id), "kind": principal.kind.value,
                   "tenant_kind": principal.tenant_kind.value, "subject": principal.external_subject,
                   "capability": capability_id, "action": action_id, "params": payload.params,
                   "request_id": request_id}
        try:
            return await run_worker_command(command)
        except WorkerCommandConflict as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        except (WorkflowFailureError, TemporalWorkerUnavailable, RPCError) as exc:
            raise HTTPException(503, detail="CAD Skills worker could not complete this request") from exc
    return await perform_action(owner, capability_id, action_id, payload.params, payload.request_id,
                                getattr(request.app.state, "execution_backend", None))
