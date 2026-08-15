"""Optional minimum-scope APS OAuth v2 and Data Management client."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from .errors import FusionConnectorError
from .token_store import EncryptedTokenStore

APS_BASE = "https://developer.api.autodesk.com"
AUTHORIZE_URL = APS_BASE + "/authentication/v2/authorize"
TOKEN_URL = APS_BASE + "/authentication/v2/token"
DATA_BASE = APS_BASE + "/data/v1"
MINIMUM_SCOPE = "data:read"


@dataclass(frozen=True)
class ApsConfig:
    enabled: bool
    client_id: str
    client_secret: str
    redirect_uri: str


class ApsClient:
    def __init__(
        self,
        config: ApsConfig,
        store: Any | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.config = config
        self.store = store
        self.transport = transport

    def _require_configured(self) -> Any:
        if not self.config.enabled:
            raise FusionConnectorError("CLOUD_NOT_CONFIGURED", "Autodesk Platform Services integration is disabled")
        if not all((self.config.client_id, self.config.client_secret, self.config.redirect_uri, self.store)):
            raise FusionConnectorError("CLOUD_NOT_CONFIGURED", "Autodesk Platform Services configuration is incomplete")
        return self.store

    def start_oauth(self, owner_id: str) -> dict[str, str]:
        store = self._require_configured()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        store.create_state(state, owner_id, verifier)
        query = urlencode({
            "response_type": "code", "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri, "scope": MINIMUM_SCOPE,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        })
        return {"authorization_url": f"{AUTHORIZE_URL}?{query}", "state": state}

    async def start_oauth_async(self, owner_id: str) -> dict[str, str]:
        store = self._require_configured()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        await _maybe_await(store.create_state(state, owner_id, verifier))
        query = urlencode({
            "response_type": "code", "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri, "scope": MINIMUM_SCOPE,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        })
        return {"authorization_url": f"{AUTHORIZE_URL}?{query}", "state": state}

    async def exchange_code(self, state: str, code: str) -> str:
        store = self._require_configured()
        owner_id, verifier = await _maybe_await(store.consume_state(state))
        token = await self._token_request({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": self.config.redirect_uri, "code_verifier": verifier,
        })
        normalized = self._normalize_token(token)
        if hasattr(store, "put_after_state"):
            await _maybe_await(store.put_after_state(state, normalized))
        else:
            await _maybe_await(store.put(owner_id, normalized))
        return owner_id

    async def status(self, owner_id: str) -> dict[str, Any]:
        if not self.config.enabled:
            return {"enabled": False, "connected": False, "scope": None}
        store = self._require_configured()
        token = await _maybe_await(store.get(owner_id))
        return {"enabled": True, "connected": token is not None, "scope": token.get("scope") if token else None}

    async def delete_token(self, owner_id: str) -> None:
        await _maybe_await(self._require_configured().delete(owner_id))

    async def hubs(self, owner_id: str) -> dict[str, Any]:
        return await self._data_get(owner_id, "/hubs")

    async def projects(self, owner_id: str, hub_id: str) -> dict[str, Any]:
        return await self._data_get(owner_id, f"/hubs/{_segment(hub_id)}/projects")

    async def top_folders(self, owner_id: str, hub_id: str, project_id: str) -> dict[str, Any]:
        return await self._data_get(owner_id, f"/hubs/{_segment(hub_id)}/projects/{_segment(project_id)}/topFolders")

    async def folder_contents(self, owner_id: str, project_id: str, folder_id: str) -> dict[str, Any]:
        return await self._data_get(owner_id, f"/projects/{_segment(project_id)}/folders/{_segment(folder_id)}/contents")

    async def item_versions(self, owner_id: str, project_id: str, item_id: str) -> dict[str, Any]:
        return await self._data_get(owner_id, f"/projects/{_segment(project_id)}/items/{_segment(item_id)}/versions")

    async def version(self, owner_id: str, project_id: str, version_id: str) -> dict[str, Any]:
        return await self._data_get(owner_id, f"/projects/{_segment(project_id)}/versions/{_segment(version_id)}")

    async def _access_token(self, owner_id: str) -> str:
        store = self._require_configured()
        token = await _maybe_await(store.get(owner_id))
        if not token:
            raise FusionConnectorError("CLOUD_AUTH_REQUIRED", "Connect an Autodesk account first")
        if float(token.get("expires_at", 0)) <= time.time() + 60:
            refresh_token = token.get("refresh_token")
            if not refresh_token:
                raise FusionConnectorError("CLOUD_AUTH_REQUIRED", "Autodesk access token expired")
            refreshed = await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
            if not refreshed.get("refresh_token"):
                refreshed["refresh_token"] = refresh_token
            token = self._normalize_token(refreshed)
            await _maybe_await(store.put(owner_id, token))
        return token["access_token"]

    async def _token_request(self, fields: dict[str, str]) -> dict[str, Any]:
        auth = (self.config.client_id, self.config.client_secret)
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=20.0) as client:
                response = await client.post(TOKEN_URL, data=fields, auth=auth)
        except httpx.HTTPError as exc:
            raise FusionConnectorError("CLOUD_AUTH_REQUIRED", "Autodesk OAuth request failed") from exc
        if response.is_error:
            raise FusionConnectorError("CLOUD_AUTH_REQUIRED", "Autodesk OAuth rejected the request")
        try:
            return response.json()
        except ValueError as exc:
            raise FusionConnectorError("CLOUD_AUTH_REQUIRED", "Autodesk OAuth returned invalid JSON") from exc

    async def _data_get(self, owner_id: str, path: str) -> dict[str, Any]:
        token = await self._access_token(owner_id)
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(transport=self.transport, timeout=20.0) as client:
                    response = await client.get(DATA_BASE + path, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                raise FusionConnectorError("FUSION_API_ERROR", "Autodesk Data Management request failed") from exc
            if response.status_code != 429:
                break
            if attempt == 1:
                raise FusionConnectorError("CLOUD_RATE_LIMITED", "Autodesk Data Management rate limit exceeded")
            delay = min(max(float(response.headers.get("Retry-After", "1")), 0), 5)
            await asyncio.sleep(delay)
        if response.status_code == 401:
            raise FusionConnectorError("CLOUD_AUTH_REQUIRED", "Autodesk authorization is no longer valid")
        if response.status_code == 429:
            raise FusionConnectorError("CLOUD_RATE_LIMITED", "Autodesk Data Management rate limit exceeded")
        if response.is_error:
            raise FusionConnectorError("FUSION_API_ERROR", "Autodesk Data Management request failed")
        try:
            return response.json()
        except ValueError as exc:
            raise FusionConnectorError("FUSION_API_ERROR", "Autodesk Data Management returned invalid JSON") from exc

    @staticmethod
    def _normalize_token(token: dict[str, Any]) -> dict[str, Any]:
        if not token.get("access_token"):
            raise FusionConnectorError("CLOUD_AUTH_REQUIRED", "Autodesk OAuth response did not contain an access token")
        scope = str(token.get("scope", MINIMUM_SCOPE))
        if MINIMUM_SCOPE not in scope.split():
            raise FusionConnectorError("CLOUD_AUTH_REQUIRED", "Autodesk token is missing the minimum data:read scope")
        return {
            "access_token": token["access_token"],
            "refresh_token": token.get("refresh_token"),
            "token_type": token.get("token_type", "Bearer"),
            "scope": scope,
            "expires_at": time.time() + int(token.get("expires_in", 3600)),
        }


def _segment(value: str) -> str:
    return quote(value, safe="")


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value
