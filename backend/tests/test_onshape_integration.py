import asyncio

import pytest

from app.config import settings
from app.integrations.onshape.client import OnshapeClient
from app.integrations.onshape.service import OnshapeService
from app.models.schemas import OnshapePublishRequest
from app.storage import history


def test_onshape_signature_payload_is_canonical_lowercase():
    payload = OnshapeClient._signature_payload(
        "GET",
        "ABC123",
        "Sun, 19 Jul 2026 12:00:00 GMT",
        "application/json",
        "/api/documents",
        "limit=20",
    )
    assert payload == "get\nabc123\nsun, 19 jul 2026 12:00:00 gmt\napplication/json\n/api/documents\nlimit=20\n"


class FakeOnshapeClient:
    async def create_document(self, name, description=None, is_public=False):
        return {"id": "did-1", "name": name, "defaultWorkspace": {"id": "wid-1"}}

    async def translate_step_file(self, document_id, workspace_id, step_path, *, import_in_background=True):
        assert document_id == "did-1"
        assert workspace_id == "wid-1"
        assert step_path.name == "result.step"
        return {"id": "tid-1", "requestState": "ACTIVE"}

    async def get_translation(self, translation_id):
        assert translation_id == "tid-1"
        return {"id": "tid-1", "requestState": "DONE", "resultElementIds": ["eid-1"]}


def test_publish_step_records_onshape_link(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    monkeypatch.setattr(settings, "onshape_base_url", "https://cad.onshape.com")
    asyncio.run(history.close_db())
    request_dir = tmp_path / "files" / "req-1"
    request_dir.mkdir(parents=True)
    (request_dir / "result.step").write_text("ISO-10303-21;", encoding="utf-8")

    service = OnshapeService(client=FakeOnshapeClient())
    response = asyncio.run(service.publish_step(OnshapePublishRequest(request_id="req-1"), user_id="user-1"))

    assert response.document_id == "did-1"
    assert response.workspace_id == "wid-1"
    assert response.element_id is None
    assert response.translation_id == "tid-1"
    assert response.onshape_url.endswith("/documents/did-1/w/wid-1")
    links = asyncio.run(history.get_onshape_links("req-1", user_id="user-1"))
    assert links[0]["translation_id"] == "tid-1"
    assert links[0]["step_filename"] == "result.step"
    asyncio.run(history.close_db())


def test_refresh_latest_link_updates_translation_status(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    monkeypatch.setattr(settings, "onshape_base_url", "https://cad.onshape.com")
    asyncio.run(history.close_db())
    request_dir = tmp_path / "files" / "req-refresh"
    request_dir.mkdir(parents=True)
    (request_dir / "result.step").write_text("ISO-10303-21;", encoding="utf-8")

    service = OnshapeService(client=FakeOnshapeClient())
    asyncio.run(service.publish_step(OnshapePublishRequest(request_id="req-refresh"), user_id="user-1"))
    refreshed = asyncio.run(service.refresh_latest_link("req-refresh", user_id="user-1"))

    assert refreshed["status"] == "DONE"
    assert refreshed["element_id"] == "eid-1"
    assert refreshed["onshape_url"].endswith("/documents/did-1/w/wid-1/e/eid-1")
    asyncio.run(history.close_db())


def test_publish_existing_document_requires_workspace_id(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    request_dir = tmp_path / "files" / "req-2"
    request_dir.mkdir(parents=True)
    (request_dir / "result.step").write_text("ISO-10303-21;", encoding="utf-8")

    service = OnshapeService(client=FakeOnshapeClient())
    with pytest.raises(ValueError, match="workspace_id"):
        asyncio.run(service.publish_step(OnshapePublishRequest(request_id="req-2", document_id="did-existing")))
