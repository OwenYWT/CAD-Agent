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

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.auth import rate_limiter, verify_api_key
from app.capabilities.artifacts import ArtifactPathError, ArtifactStore, sha256_file
from app.capabilities.registry import get_capability
from app.capabilities.runtime import CapabilityRuntime, RuntimeConfig
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


def _owner_root(api_key: str | None) -> Path:
    root = Path(settings.file_storage_dir).expanduser().resolve()
    owner = (root / "capability_users" / _owner_id(api_key)).resolve()
    owner.mkdir(parents=True, exist_ok=True)
    return owner


def _runtime(api_key: str | None) -> CapabilityRuntime:
    owner = _owner_root(api_key)
    return CapabilityRuntime(
        RuntimeConfig(
            workspace_root=owner,
            artifact_root=owner / "runs",
            isolated_executor=tuple(settings.cadskills_isolated_executor) or None,
            allow_bambu_lan=settings.cadskills_enable_bambu_lan,
        )
    )


def _safe_upload_name(raw_name: str | None) -> str:
    name = Path(raw_name or "artifact.bin").name
    if name != (raw_name or "artifact.bin") or not _SAFE_FILENAME.fullmatch(name):
        raise HTTPException(status_code=400, detail="Filename must use letters, digits, '.', '_' or '-'.")
    lower = name.lower()
    if not any(lower.endswith(suffix) for suffix in _ALLOWED_UPLOAD_SUFFIXES):
        raise HTTPException(status_code=400, detail="File type is not supported by a CAD Skills workflow.")
    return name


def _public_result(result: dict[str, Any], api_key: str | None) -> dict[str, Any]:
    owner = _owner_root(api_key)
    storage = Path(settings.file_storage_dir).expanduser().resolve()
    repo = Path(__file__).resolve().parents[3]

    def clean_text(value: str) -> str:
        return (
            value.replace(str(owner), "$WORKSPACE")
            .replace(str(storage), "$WORKSPACE")
            .replace(str(repo), "$APP")
        )

    def clean_value(value: Any) -> Any:
        if isinstance(value, str):
            return clean_text(value)
        if isinstance(value, list):
            return [clean_value(item) for item in value]
        if isinstance(value, dict):
            return {key: clean_value(item) for key, item in value.items()}
        return value

    public_files: list[dict[str, Any]] = []
    for item in result.get("files") or []:
        public = {key: value for key, value in item.items() if key != "path"}
        relative = str(public.get("relative_path") or "")
        parts = Path(relative).parts
        if len(parts) >= 2:
            request_id, filename = parts[0], parts[-1]
            public["url"] = f"/api/capability-artifacts/runs/{request_id}/{filename}"
            public["input"] = f"runs/{relative}"
        public_files.append(public)
    result["files"] = public_files
    result["command_preview"] = [clean_text(str(part)) for part in result.get("command_preview") or []]
    result["data"] = clean_value(result.get("data"))
    return result


@router.post("/api/capability-artifacts", status_code=201)
async def upload_capability_artifact(
    request: Request,
    file: UploadFile = File(...),
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    filename = _safe_upload_name(file.filename)
    upload_id = uuid.uuid4().hex
    owner = _owner_root(api_key)
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
    finally:
        await file.close()

    digest = sha256_file(target)
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
    owner = _owner_root(api_key)
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
    if get_capability(capability_id) is None:
        raise HTTPException(status_code=404, detail=f"Capability '{capability_id}' not found")
    runtime = _runtime(api_key)
    result = await asyncio.to_thread(
        runtime.execute,
        capability_id,
        action_id,
        payload.params,
        payload.request_id,
    )
    return _public_result(result, api_key)
