import asyncio
import re
import time
from pathlib import Path
from typing import Any

from app.config import settings
from app.integrations.onshape.client import OnshapeClient
from app.models.schemas import (
    OnshapeCreateDocumentRequest,
    OnshapeDocumentResponse,
    OnshapeDocumentsResponse,
    OnshapePublishRequest,
    OnshapePublishResponse,
)
from app.storage import history

_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
_SAFE_FILENAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


class OnshapeService:
    def __init__(self, client: OnshapeClient | None = None):
        self.client = client or OnshapeClient()

    async def list_documents(self, q: str | None = None, offset: int = 0, limit: int = 20) -> OnshapeDocumentsResponse:
        raw = await self.client.list_documents(q=q, offset=offset, limit=limit)
        documents = raw.get("items") or raw.get("documents") or raw.get("data") or []
        return OnshapeDocumentsResponse(documents=documents if isinstance(documents, list) else [], raw=raw)

    async def create_document(self, req: OnshapeCreateDocumentRequest) -> OnshapeDocumentResponse:
        raw = await self.client.create_document(
            req.name,
            description=req.description,
            is_public=settings.onshape_default_document_public if req.is_public is None else req.is_public,
        )
        document_id = _extract_document_id(raw)
        workspace_id = _extract_workspace_id(raw)
        if not document_id:
            raise ValueError("Onshape create document response did not include a document id")
        return OnshapeDocumentResponse(
            id=document_id,
            name=str(raw.get("name") or req.name),
            default_workspace_id=workspace_id,
            web_url=_build_onshape_url(document_id, workspace_id),
            raw=raw,
        )

    async def publish_step(self, req: OnshapePublishRequest, user_id: str | None = None) -> OnshapePublishResponse:
        step_path = _find_step_file(req.request_id, req.step_filename)
        document_raw: dict[str, Any] = {}
        if req.document_id:
            document_id = req.document_id
            workspace_id = req.workspace_id
            document_name = req.document_name or ""
            if not workspace_id:
                raise ValueError("workspace_id is required when publishing to an existing Onshape document")
        else:
            created = await self.create_document(
                OnshapeCreateDocumentRequest(
                    name=req.document_name or f"CAD-Agent {req.request_id}",
                    is_public=settings.onshape_default_document_public,
                )
            )
            document_id = created.id
            workspace_id = created.default_workspace_id
            document_name = created.name
            document_raw = created.raw
            if not workspace_id:
                raise ValueError("Onshape create document response did not include a default workspace id")

        translation_raw = await self.client.translate_step_file(document_id, workspace_id, step_path)
        translation_id = _extract_translation_id(translation_raw)
        status_raw = translation_raw
        if req.wait_for_completion and translation_id:
            status_raw = await self._poll_translation(translation_id, req.timeout_s, req.poll_interval_s)
        status = _extract_translation_status(status_raw)
        element_id = _extract_element_id(status_raw) or _extract_element_id(translation_raw)
        onshape_url = _build_onshape_url(document_id, workspace_id, element_id)
        raw = {"document": document_raw, "translation": translation_raw, "status": status_raw}

        await history.save_onshape_link(
            request_id=req.request_id,
            user_id=user_id,
            document_id=document_id,
            workspace_id=workspace_id,
            element_id=element_id,
            translation_id=translation_id,
            status=status,
            onshape_url=onshape_url,
            document_name=document_name,
            step_filename=step_path.name,
            mode="import_step",
            raw_response=raw,
        )
        return OnshapePublishResponse(
            request_id=req.request_id,
            status=status,
            onshape_url=onshape_url,
            document_id=document_id,
            workspace_id=workspace_id,
            element_id=element_id,
            translation_id=translation_id,
            document_name=document_name,
            step_filename=step_path.name,
            raw=raw,
        )

    async def _poll_translation(self, translation_id: str, timeout_s: float, poll_interval_s: float) -> dict:
        deadline = time.monotonic() + timeout_s
        latest: dict[str, Any] = {}
        while time.monotonic() <= deadline:
            latest = await self.client.get_translation(translation_id)
            status = _extract_translation_status(latest).lower()
            if status in {"done", "failed", "cancelled", "canceled"}:
                return latest
            await asyncio.sleep(poll_interval_s)
        return latest or {"requestState": "TIMEOUT", "id": translation_id}

    async def get_translation(self, translation_id: str) -> dict:
        return await self.client.get_translation(translation_id)

    async def get_links(self, request_id: str, user_id: str | None = None) -> list[dict]:
        return await history.get_onshape_links(request_id, user_id=user_id)

    async def refresh_latest_link(self, request_id: str, user_id: str | None = None) -> dict:
        link = await history.get_latest_onshape_link(request_id, user_id=user_id)
        if not link:
            raise FileNotFoundError(f"No Onshape link found for request_id={request_id}")
        translation_id = link.get("translation_id")
        if not translation_id:
            return _public_link(link)
        status_raw = await self.client.get_translation(str(translation_id))
        status = _extract_translation_status(status_raw)
        element_id = _extract_element_id(status_raw) or link.get("element_id")
        onshape_url = _build_onshape_url(link["document_id"], link["workspace_id"], element_id)
        updated = await history.update_onshape_link_status(
            int(link["id"]),
            status=status,
            element_id=element_id,
            onshape_url=onshape_url,
            raw_response=status_raw,
        )
        return _public_link(updated or link)


def _find_step_file(request_id: str, filename: str | None = None) -> Path:
    if not _SAFE_ID_PATTERN.match(request_id):
        raise ValueError("Invalid request_id")
    storage_dir = Path(settings.file_storage_dir).resolve()
    request_dir = (storage_dir / request_id).resolve()
    if not str(request_dir).startswith(str(storage_dir)):
        raise ValueError("Invalid request_id path")
    if not request_dir.exists():
        raise FileNotFoundError(f"Generated file directory not found for request_id={request_id}")
    if filename:
        if not _SAFE_FILENAME_PATTERN.match(filename) or ".." in filename or "/" in filename or "\\" in filename:
            raise ValueError("Invalid step_filename")
        candidate = (request_dir / filename).resolve()
        if not str(candidate).startswith(str(request_dir)):
            raise ValueError("Invalid step_filename path")
        if candidate.suffix.lower() not in {".step", ".stp"}:
            raise ValueError("step_filename must be a .step or .stp file")
        if not candidate.exists():
            raise FileNotFoundError(f"STEP file not found: {filename}")
        return candidate
    candidates = sorted([*request_dir.glob("*.step"), *request_dir.glob("*.stp")])
    if not candidates:
        raise FileNotFoundError(f"No STEP file found for request_id={request_id}")
    return candidates[0]


def _extract_document_id(raw: dict[str, Any]) -> str | None:
    value = raw.get("id") or raw.get("documentId") or raw.get("did")
    return str(value) if value else None


def _extract_workspace_id(raw: dict[str, Any]) -> str | None:
    default_workspace = raw.get("defaultWorkspace")
    if isinstance(default_workspace, dict):
        value = default_workspace.get("id") or default_workspace.get("workspaceId")
        if value:
            return str(value)
    value = raw.get("defaultWorkspaceId") or raw.get("workspaceId") or raw.get("wid")
    return str(value) if value else None


def _extract_translation_id(raw: dict[str, Any]) -> str | None:
    value = raw.get("id") or raw.get("translationId") or raw.get("tid")
    return str(value) if value else None


def _extract_translation_status(raw: dict[str, Any]) -> str:
    value = raw.get("requestState") or raw.get("state") or raw.get("status")
    return str(value or "submitted")


def _extract_element_id(raw: dict[str, Any]) -> str | None:
    for key in ("resultElementIds", "elementIds"):
        value = raw.get(key)
        if isinstance(value, list) and value:
            return str(value[0])
    value = raw.get("elementId") or raw.get("eid")
    return str(value) if value else None


def _build_onshape_url(document_id: str, workspace_id: str | None = None, element_id: str | None = None) -> str:
    url = f"{settings.onshape_base_url.rstrip('/')}/documents/{document_id}"
    if workspace_id:
        url = f"{url}/w/{workspace_id}"
    if workspace_id and element_id:
        url = f"{url}/e/{element_id}"
    return url


def _public_link(link: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in link.items() if key != "id"}
