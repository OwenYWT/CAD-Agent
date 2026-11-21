from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
import hashlib

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import pytest

from app.api import files as files_api
from app.config import settings


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [False, True])
async def test_durable_bom_csv_artifact_is_downloadable(monkeypatch, corrupt) -> None:
    payload = b"Index,Name,Quantity,File Name\n1,base,1,assembly.FCStd\n"

    async def belongs_to(_request_id: str, _credential: str | None) -> bool:
        return True

    async def project_file(_request_id: str, _filename: str) -> dict:
        return {
            "object_key": "committed/bom.csv",
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "content_type": "text/csv; charset=utf-8",
        }

    client = SimpleNamespace(get_object=lambda **_: {"Body": BytesIO(
        b"x" * len(payload) if corrupt else payload)})
    monkeypatch.setattr(settings, "durable_control_plane_enabled", True)
    monkeypatch.setattr(files_api, "request_belongs_to", belongs_to)
    monkeypatch.setattr(files_api, "get_project_file", project_file)
    monkeypatch.setattr("app.object_store.get_object_store_client", lambda: client)
    app = FastAPI()
    app.include_router(files_api.router)
    app.dependency_overrides[files_api.verify_api_key] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.get("/files/workflow-1/bom.csv")
    if corrupt:
        assert response.status_code == 503
        assert "checksum" in response.json()["detail"]
    else:
        assert response.status_code == 200
        assert response.content == payload
        assert response.headers["content-type"] == "text/csv; charset=utf-8"
        assert response.headers["content-disposition"] == 'attachment; filename="bom.csv"'
