"""Backend-side CadAdapter implementation for the loopback Fusion Runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from .contract import (
    ACTION_NAMES,
    CONTEXT_SECTIONS,
    ActionData,
    CadAction,
    CadCapabilities,
    CadResult,
    ContextData,
    ContextRequest,
    VerificationData,
    VerifyRequest,
)
from .errors import FusionConnectorError


class RemoteFusionAdapter:
    """Typed network adapter. It never imports or emulates Autodesk APIs."""

    def __init__(
        self,
        *,
        base_url: str,
        backend_secret: str,
        owner_id: str,
        timeout_s: float = 35.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        parsed = urlparse(base_url)
        if transport is None and (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise FusionConnectorError("CONNECTOR_OFFLINE", "Fusion Runtime URL must use loopback HTTP")
        self.base_url = base_url.rstrip("/")
        self.backend_secret = backend_secret
        self.owner_id = owner_id
        self.timeout_s = timeout_s
        self.transport = transport
        self._capabilities = CadCapabilities(
            actions=ACTION_NAMES,
            context_sections=CONTEXT_SECTIONS,
            limitations=[
                "Fusion Desktop and its Add-in must be running on the Runtime host",
                "Cloud version completion may remain pending after a local save is accepted",
                "Started mutations are not automatically replayed after connection loss",
            ],
        )

    def get_capabilities(self) -> CadCapabilities:
        """Return cached/static information only; this method never performs I/O."""
        return self._capabilities.model_copy(deep=True)

    async def refresh_status(self) -> CadCapabilities:
        try:
            status = await self._request("GET", "/v1/backend/status")
        except FusionConnectorError as exc:
            if exc.code == "CONNECTOR_OFFLINE":
                self._capabilities.available = False
                self._capabilities.runtime_online = False
                self._capabilities.connector_online = False
                self._capabilities.fusion_running = None
                return self.get_capabilities()
            raise
        connectors = [item for item in status.get("connectors", []) if item.get("online")]
        selected = connectors[0] if len(connectors) == 1 else None
        self._capabilities.runtime_online = True
        self._capabilities.connector_online = bool(connectors)
        self._capabilities.fusion_running = True if connectors else None
        self._capabilities.available = selected is not None
        if selected:
            heartbeat = selected.get("last_heartbeat")
            self._capabilities.last_heartbeat_at = datetime.fromtimestamp(heartbeat, tz=timezone.utc) if heartbeat else None
            self._capabilities.fusion_version = selected.get("fusion_version")
            remote = selected.get("capabilities") or {}
            self._capabilities.actions = remote.get("actions", ACTION_NAMES)
            self._capabilities.context_sections = remote.get("context_sections", CONTEXT_SECTIONS)
        return self.get_capabilities()

    async def get_context(self, query: ContextRequest) -> CadResult[ContextData]:
        response = await self._submit("context", query.model_dump(mode="json"))
        return CadResult[ContextData].model_validate(response)

    async def execute(self, action: CadAction) -> CadResult[ActionData]:
        response = await self._submit("execute", action.model_dump(mode="json"))
        return CadResult[ActionData].model_validate(response)

    async def verify(self, specification: VerifyRequest) -> CadResult[VerificationData]:
        response = await self._submit("verify", specification.model_dump(mode="json"))
        return CadResult[VerificationData].model_validate(response)

    async def request_status(self, request_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/backend/requests/{request_id}", headers={"X-Owner-ID": self.owner_id}
        )

    async def cancel(self, request_id: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/v1/backend/requests/{request_id}/cancel", headers={"X-Owner-ID": self.owner_id}
        )

    async def _submit(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/backend/tasks",
            json={"owner_id": self.owner_id, "operation": operation, "payload": payload, "wait": True},
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.backend_secret}", **kwargs.pop("headers", {})}
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_s,
                transport=self.transport,
            ) as client:
                response = await client.request(method, path, headers=headers, **kwargs)
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            raise FusionConnectorError("CONNECTOR_OFFLINE", "Fusion Local Runtime is offline") from exc
        except httpx.TimeoutException as exc:
            raise FusionConnectorError("REQUEST_TIMEOUT", "Fusion Local Runtime request timed out") from exc
        if response.is_error:
            try:
                body = response.json()
                remote = body.get("error", {})
                code = remote.get("code", "FUSION_API_ERROR")
                message = remote.get("message", "Fusion Runtime request failed")
                details = remote.get("details", {})
                raise FusionConnectorError(code, message, details=details)
            except ValueError as exc:
                raise FusionConnectorError("FUSION_API_ERROR", "Fusion Runtime returned an invalid error response") from exc
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise FusionConnectorError("FUSION_API_ERROR", "Fusion Runtime returned invalid JSON") from exc
