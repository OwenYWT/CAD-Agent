import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import auth as auth_module
from app.api.onshape import _http_error
from app.config import settings
from app.integrations.onshape.client import OnshapeAPIError, OnshapeClient
from app.integrations.onshape.service import OnshapeService, _find_step_file
from app.models.schemas import OnshapePublishRequest
from app.storage import history
from app.storage.file_ownership import claim_request_owner
from app.main import create_app


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


@pytest.mark.parametrize("upstream_status", [401, 403, 500, 503])
def test_onshape_upstream_failures_do_not_invalidate_app_session(upstream_status):
    error = _http_error(OnshapeAPIError(upstream_status, {"message": "upstream failure"}))
    assert error.status_code == 502
    assert error.detail["status_code"] == upstream_status


@pytest.mark.asyncio
async def test_onshape_network_failure_is_reported_as_upstream_error(monkeypatch):
    async def fail_send(_client, request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fail_send)
    client = OnshapeClient(access_key="access", secret_key="secret")

    with pytest.raises(OnshapeAPIError) as exc_info:
        await client.list_documents()
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["type"] == "ConnectError"


def test_onshape_placeholder_credentials_are_not_treated_as_configured(monkeypatch):
    monkeypatch.setattr(settings, "onshape_access_key", "<ONSHAPE_ACCESS_KEY>")
    monkeypatch.setattr(settings, "onshape_secret_key", "<ONSHAPE_SECRET_KEY>")

    assert settings.has_onshape_credentials is False
    assert OnshapeClient().configured is False


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


def test_onshape_links_are_isolated_by_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    asyncio.run(history.close_db())
    asyncio.run(history.save_onshape_link(
        request_id="req-private",
        user_id="user-1",
        document_id="did-private",
        workspace_id="wid-private",
        element_id=None,
        translation_id="tid-private",
        status="ACTIVE",
        onshape_url="https://cad.onshape.com/documents/did-private/w/wid-private",
    ))

    assert len(asyncio.run(history.get_onshape_links("req-private", user_id="user-1"))) == 1
    assert asyncio.run(history.get_onshape_links("req-private", user_id="user-2")) == []
    assert asyncio.run(history.get_onshape_link_by_translation("tid-private", user_id="user-2")) is None
    asyncio.run(history.close_db())


def test_find_step_file_rejects_symlink_outside_request(tmp_path, monkeypatch):
    storage = tmp_path / "files"
    request_dir = storage / "req-symlink"
    request_dir.mkdir(parents=True)
    outside = tmp_path / "outside.step"
    outside.write_text("private", encoding="utf-8")
    (request_dir / "result.step").symlink_to(outside)
    monkeypatch.setattr(settings, "file_storage_dir", str(storage))

    with pytest.raises(FileNotFoundError, match="No STEP file"):
        _find_step_file("req-symlink")
    with pytest.raises(ValueError, match="path"):
        _find_step_file("req-symlink", "result.step")


@pytest.mark.auth
def test_publish_api_rejects_artifact_owned_by_another_key(tmp_path, monkeypatch):
    storage = tmp_path / "files"
    request_dir = storage / "req-owned"
    request_dir.mkdir(parents=True)
    (request_dir / "result.step").write_text("ISO-10303-21;", encoding="utf-8")
    monkeypatch.setattr(settings, "file_storage_dir", str(storage))
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "auth_token_secret", "test-only-onshape-auth-secret-with-sufficient-length")
    monkeypatch.setattr(settings, "api_keys", ["owner-key", "other-key"])
    monkeypatch.setattr(settings, "onshape_access_key", None)
    monkeypatch.setattr(settings, "onshape_secret_key", None)
    claim_request_owner("req-owned", "owner-key")
    auth_module.rate_limiter._windows.clear()
    client = TestClient(create_app())

    other = client.post(
        "/api/onshape/publish",
        headers={"Authorization": "Bearer other-key"},
        json={"request_id": "req-owned", "step_filename": "result.step"},
    )
    owner = client.post(
        "/api/onshape/publish",
        headers={"Authorization": "Bearer owner-key"},
        json={"request_id": "req-owned", "step_filename": "result.step"},
    )
    config = client.get(
        "/api/onshape/config",
        headers={"Authorization": "Bearer owner-key"},
    )

    assert other.status_code == 404
    assert owner.status_code == 503
    assert "ONSHAPE_ACCESS_KEY" in owner.json()["detail"]
    assert config.status_code == 200
    assert config.json()["configured"] is False
    auth_module.rate_limiter._windows.clear()
