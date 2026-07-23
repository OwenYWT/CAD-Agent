"""Standalone loopback FastAPI application used by Backend and Fusion Add-in."""

from __future__ import annotations

import hmac
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import Field

from .artifacts import ArtifactStore
from .contract import (
    ExecutionContext,
    LeaseIdentity,
    PROTOCOL_VERSION,
    RegisterRequest,
    RegisterResponse,
    StrictModel,
    TaskLease,
    TaskResultEnvelope,
)
from .errors import FusionConnectorError, map_exception
from .runtime_service import RuntimeService
from .runtime_store import RuntimeStore

MAX_REQUEST_BYTES = 1024 * 1024
MAX_CONNECTOR_RESULT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class RuntimeConfig:
    database_path: Path
    artifact_root: Path
    backend_secret: str
    connector_secret: str
    max_queue: int = 1_000

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        config = cls(
            database_path=Path(os.environ.get("FUSION_RUNTIME_DB", "./data/fusion360/runtime.db")),
            artifact_root=Path(os.environ.get("FUSION_ARTIFACT_ROOT", "./data/fusion360/artifacts")),
            backend_secret=os.environ.get("FUSION_RUNTIME_BACKEND_SECRET", ""),
            connector_secret=os.environ.get("FUSION_RUNTIME_CONNECTOR_SECRET", ""),
            max_queue=int(os.environ.get("FUSION_RUNTIME_MAX_QUEUE", "1000")),
        )
        if len(config.backend_secret) < 32 or len(config.connector_secret) < 32:
            raise RuntimeError("Fusion Runtime role secrets must each contain at least 32 characters")
        return config


class BackendSubmit(StrictModel):
    owner_id: str = Field(min_length=1, max_length=256)
    operation: Literal["context", "execute", "verify"]
    payload: dict[str, Any]
    wait: bool = True


class ConnectorHeartbeat(StrictModel):
    connector_instance_id: str


def create_runtime_app(config: RuntimeConfig | None = None) -> FastAPI:
    config = config or RuntimeConfig.from_env()
    if not config.backend_secret or not config.connector_secret:
        raise RuntimeError("Both Fusion Runtime role secrets are required")
    if hmac.compare_digest(config.backend_secret, config.connector_secret):
        raise RuntimeError("Fusion Runtime backend and connector secrets must differ")
    store = RuntimeStore(config.database_path, max_queue=config.max_queue)
    artifacts = ArtifactStore(config.artifact_root)
    service = RuntimeService(store, artifacts)
    app = FastAPI(title="CAD Agent Fusion 360 Local Runtime", version="1.0.0")
    app.state.store = store
    app.state.artifacts = artifacts
    app.state.service = service

    @app.middleware("http")
    async def request_size_limit(request: Request, call_next):
        is_connector_result = (
            request.method == "POST"
            and request.url.path.startswith("/v1/connector/tasks/")
            and request.url.path.endswith("/result")
        )
        limit = MAX_CONNECTOR_RESULT_BYTES if is_connector_result else MAX_REQUEST_BYTES
        value = request.headers.get("content-length")
        try:
            declared_size = int(value) if value else 0
        except ValueError:
            error = FusionConnectorError("INVALID_ACTION", "Invalid Content-Length")
            return JSONResponse(status_code=400, content={"error": error.to_model().model_dump(mode="json")})
        if declared_size > limit:
            error = FusionConnectorError("INVALID_ACTION", "Request body exceeds its configured limit")
            return JSONResponse(status_code=413, content={"error": error.to_model().model_dump(mode="json")})
        if request.method in {"POST", "PUT", "PATCH"}:
            body = await request.body()
            if len(body) > limit:
                error = FusionConnectorError("INVALID_ACTION", "Request body exceeds its configured limit")
                return JSONResponse(status_code=413, content={"error": error.to_model().model_dump(mode="json")})
        return await call_next(request)

    @app.exception_handler(FusionConnectorError)
    async def connector_error_handler(_request: Request, exc: FusionConnectorError):
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_model().model_dump(mode="json")})

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(_request: Request, exc: RequestValidationError):
        error = FusionConnectorError(
            "INVALID_ACTION",
            "Fusion Runtime request validation failed",
            details={"errors": exc.errors()},
        )
        return JSONResponse(status_code=error.http_status, content={"error": error.to_model().model_dump(mode="json")})

    @app.exception_handler(Exception)
    async def generic_error_handler(_request: Request, exc: Exception):
        mapped = map_exception(exc)
        return JSONResponse(status_code=mapped.http_status, content={"error": mapped.to_model().model_dump(mode="json")})

    def require_role(role: Literal["backend", "connector"]):
        expected = config.backend_secret if role == "backend" else config.connector_secret
        other = config.connector_secret if role == "backend" else config.backend_secret

        async def dependency(authorization: Annotated[str | None, Header()] = None) -> None:
            token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
            if hmac.compare_digest(token, other):
                raise FusionConnectorError("CREDENTIAL_ROLE_MISMATCH", f"Credential cannot access {role} endpoints")
            if not hmac.compare_digest(token, expected):
                error = FusionConnectorError("CREDENTIAL_ROLE_MISMATCH", "Invalid Runtime credential")
                # Invalid credentials are authentication failures; do not reveal which role exists.
                raise error

        return dependency

    backend_auth = require_role("backend")
    connector_auth = require_role("connector")

    @app.get("/health")
    async def health():
        return {"status": "ok", "protocol_version": PROTOCOL_VERSION}

    @app.get("/v1/backend/status", dependencies=[Depends(backend_auth)])
    async def status():
        connectors = store.connectors()
        return {
            "runtime_online": True,
            "artifact_root_fingerprint": artifacts.fingerprint,
            "connectors": [
                {
                    "connector_instance_id": c["connector_instance_id"],
                    "online": c["online"],
                    "last_heartbeat": c["last_heartbeat"],
                    "fusion_version": c["fusion_version"],
                    "capabilities": c["capabilities"],
                }
                for c in connectors
            ],
        }

    @app.post("/v1/backend/tasks", dependencies=[Depends(backend_auth)])
    async def submit(body: BackendSubmit):
        return await service.submit(body.owner_id, body.operation, body.payload, wait=body.wait)

    @app.get("/v1/backend/requests/{request_id}", dependencies=[Depends(backend_auth)])
    async def get_request(request_id: str, x_owner_id: Annotated[str, Header()]):
        return service.public_task(store.get_task(x_owner_id, request_id))

    @app.post("/v1/backend/requests/{request_id}/cancel", dependencies=[Depends(backend_auth)])
    async def cancel_request(request_id: str, x_owner_id: Annotated[str, Header()]):
        return service.public_task(store.request_cancel(x_owner_id, request_id))

    @app.get("/v1/backend/artifacts/{request_id}/{filename}", dependencies=[Depends(backend_auth)])
    async def download_artifact(request_id: str, filename: str, x_owner_id: Annotated[str, Header()]):
        task = store.get_task(x_owner_id, request_id)
        result = task.get("result") or {}
        match = next((a for a in result.get("artifacts", []) if a.get("filename") == filename), None)
        if not match:
            raise FusionConnectorError("ARTIFACT_INVALID", "Artifact was not found")
        path = artifacts.resolve_local(x_owner_id, request_id, filename)
        artifacts.validate_file(path, match["kind"])
        return FileResponse(path, filename=filename, media_type=match["media_type"])

    @app.post("/v1/connector/register", response_model=RegisterResponse, dependencies=[Depends(connector_auth)])
    async def register(body: RegisterRequest):
        if body.artifact_root_fingerprint != artifacts.fingerprint:
            raise FusionConnectorError("ARTIFACT_ROOT_MISMATCH", "Connector and Runtime artifact roots differ")
        return store.register_connector(body.model_dump(mode="json"))

    @app.post("/v1/connector/heartbeat", dependencies=[Depends(connector_auth)])
    async def heartbeat(body: ConnectorHeartbeat):
        store.reap()
        store.heartbeat(body.connector_instance_id)
        return {"status": "ok", "server_time": time.time()}

    @app.get("/v1/connector/{connector_id}/tasks/next", dependencies=[Depends(connector_auth)])
    async def poll_task(connector_id: str):
        task = store.lease_next(connector_id)
        if not task:
            return Response(status_code=204)
        artifact_dir = artifacts.request_dir(task["owner_id"], task["request_id"], create=True)
        lease = TaskLease(
            request_id=task["request_id"],
            operation=task["operation"],
            intent_hash=task["intent_hash"],
            lease_id=task["lease_id"],
            attempt=task["attempt"],
            leased_until=task["leased_until"],
            deadline=task["deadline"],
            payload=task["payload"],
            execution_context=ExecutionContext(
                artifact_dir=str(artifact_dir),
                artifact_root_fingerprint=artifacts.fingerprint,
            ),
        )
        return lease.model_dump(mode="json")

    @app.post("/v1/connector/tasks/{request_id}/started", dependencies=[Depends(connector_auth)])
    async def task_started(request_id: str, body: LeaseIdentity):
        if str(body.request_id) != request_id:
            raise FusionConnectorError("STALE_LEASE", "Request ID does not match the lease body")
        task = store.mark_started(
            str(body.connector_instance_id), request_id, str(body.lease_id), body.attempt, body.intent_hash
        )
        return {"status": task["status"]}

    @app.post("/v1/connector/tasks/{request_id}/result", dependencies=[Depends(connector_auth)])
    async def task_result(request_id: str, body: TaskResultEnvelope):
        if str(body.request_id) != request_id:
            raise FusionConnectorError("STALE_LEASE", "Request ID does not match the result body")
        return await service.record_result(body)

    @app.get("/v1/connector/{connector_id}/tasks/{request_id}/control", dependencies=[Depends(connector_auth)])
    async def task_control(connector_id: str, request_id: str, lease_id: str, attempt: int):
        return store.control(connector_id, request_id, lease_id, attempt)

    return app


def app_from_env() -> FastAPI:
    return create_runtime_app(RuntimeConfig.from_env())
