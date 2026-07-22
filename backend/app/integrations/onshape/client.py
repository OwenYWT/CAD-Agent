import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx

from app.config import settings


class OnshapeNotConfigured(RuntimeError):
    pass


class OnshapeAPIError(RuntimeError):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(f"Onshape API error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail

    def to_detail(self) -> dict:
        return {"type": "OnshapeAPIError", "status_code": self.status_code, "detail": self.detail}


class OnshapeClient:
    def __init__(
        self,
        base_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        timeout_s: float | None = None,
    ):
        self.base_url = (base_url or settings.onshape_base_url).rstrip("/")
        self.access_key = access_key if access_key is not None else settings.onshape_access_key
        self.secret_key = secret_key if secret_key is not None else settings.onshape_secret_key
        self.timeout_s = timeout_s if timeout_s is not None else settings.onshape_timeout_s

    @property
    def configured(self) -> bool:
        return bool(self.access_key and self.secret_key)

    def _ensure_configured(self):
        if not self.configured:
            raise OnshapeNotConfigured(settings.onshape_credentials_error)

    @staticmethod
    def _signature_payload(
        method: str,
        nonce: str,
        date: str,
        content_type: str,
        path: str,
        query: str,
    ) -> str:
        return f"{method}\n{nonce}\n{date}\n{content_type}\n{path}\n{query}\n".lower()

    @staticmethod
    def _sign(secret_key: str, payload: str) -> str:
        digest = hmac.new(secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def _apply_auth(self, request: httpx.Request):
        self._ensure_configured()
        nonce = secrets.token_hex(16)
        date = format_datetime(datetime.now(timezone.utc), usegmt=True)
        content_type = request.headers.get("content-type", "")
        raw_path = request.url.raw_path.decode("ascii")
        path = raw_path.split("?", 1)[0]
        query = request.url.query.decode("ascii")
        payload = self._signature_payload(request.method, nonce, date, content_type, path, query)
        signature = self._sign(self.secret_key or "", payload)
        request.headers["Date"] = date
        request.headers["On-Nonce"] = nonce
        request.headers["Authorization"] = f"On {self.access_key}:HmacSHA256:{signature}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict:
        headers = {"Accept": "application/vnd.onshape.v1+json"}
        if files is None:
            headers["Content-Type"] = "application/json"
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_s) as client:
            request = client.build_request(
                method.upper(),
                path,
                params=params,
                json=json_body,
                data=data,
                files=files,
                headers=headers,
            )
            self._apply_auth(request)
            response = await client.send(request)

        if response.status_code >= 400:
            try:
                detail: Any = response.json()
            except ValueError:
                detail = response.text
            raise OnshapeAPIError(response.status_code, detail)
        if not response.content:
            return {}
        try:
            parsed = response.json()
        except ValueError:
            return {"raw": response.text}
        return parsed if isinstance(parsed, dict) else {"items": parsed}

    async def list_documents(self, q: str | None = None, offset: int = 0, limit: int = 20) -> dict:
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        if q:
            params["q"] = q
        return await self.request("GET", "/api/documents", params=params)

    async def create_document(self, name: str, description: str | None = None, is_public: bool = False) -> dict:
        body: dict[str, Any] = {"name": name, "isPublic": is_public}
        if description:
            body["description"] = description
        return await self.request("POST", "/api/documents", json_body=body)

    async def list_elements(self, document_id: str, workspace_id: str) -> dict:
        return await self.request("GET", f"/api/documents/d/{document_id}/w/{workspace_id}/elements")

    async def translate_step_file(
        self,
        document_id: str,
        workspace_id: str,
        step_path: Path,
        *,
        import_in_background: bool = True,
    ) -> dict:
        data = {
            "formatName": "ONSHAPE",
            "storeInDocument": "true",
            "flattenAssemblies": "true",
        }
        if import_in_background:
            data["importInBackground"] = "true"
        with step_path.open("rb") as step_file:
            files = {"file": (step_path.name, step_file, "application/step")}
            return await self.request(
                "POST",
                f"/api/translations/d/{document_id}/w/{workspace_id}",
                data=data,
                files=files,
            )

    async def get_translation(self, translation_id: str) -> dict:
        return await self.request("GET", f"/api/translations/{translation_id}")
