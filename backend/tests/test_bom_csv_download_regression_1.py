from __future__ import annotations

import hashlib

import pytest

from app.api import files as files_api
from app.config import settings


@pytest.mark.asyncio
async def test_durable_bom_csv_artifact_is_downloadable(monkeypatch) -> None:
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

    async def object_payload(_object_key: str) -> bytes:
        return payload

    monkeypatch.setattr(settings, "durable_control_plane_enabled", True)
    monkeypatch.setattr(files_api, "request_belongs_to", belongs_to)
    monkeypatch.setattr(files_api, "get_project_file", project_file)
    monkeypatch.setattr("app.object_store.get_object", object_payload)

    response = await files_api.download_file("workflow-1", "bom.csv", None)

    assert response.body == payload
    assert response.media_type == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        'attachment; filename="bom.csv"'
    )
