import pytest

from app.models.schemas import OnshapeDocumentResponse, OnshapeDocumentsResponse, OnshapePublishResponse
from app.tools import LayeredToolRegistry, SessionToolPool, ToolContext, ToolExecutor
from app.tools.plugins.business import tool_onshape


class FakeOnshapeService:
    async def list_documents(self, q=None, offset=0, limit=20):
        assert q == "demo"
        assert offset == 0
        assert limit == 10
        return OnshapeDocumentsResponse(
            documents=[{"id": "did-1", "name": "Demo", "defaultWorkspace": {"id": "wid-1"}}],
            raw={"items": [{"id": "did-1"}]},
        )

    async def list_elements(self, document_id, workspace_id):
        assert document_id == "did-1"
        assert workspace_id == "wid-1"
        return {
            "document_id": document_id,
            "workspace_id": workspace_id,
            "elements": [{"element_id": "eid-1", "name": "Part Studio 1", "type": "PARTSTUDIO"}],
            "raw": {"items": [{"id": "eid-1"}]},
        }

    async def list_partstudio_features(self, document_id, workspace_id, element_id):
        assert document_id == "did-1"
        assert workspace_id == "wid-1"
        assert element_id == "eid-1"
        return {
            "document_id": document_id,
            "workspace_id": workspace_id,
            "element_id": element_id,
            "features": [{"id": "fid-1", "name": "Extrude 1", "feature_type": "extrude", "suppressed": False}],
            "raw": {"features": [{"featureId": "fid-1"}]},
        }

    async def create_document(self, req):
        assert req.name == "Created Doc"
        return OnshapeDocumentResponse(
            id="did-created",
            name=req.name,
            default_workspace_id="wid-created",
            web_url="https://cad.onshape.com/documents/did-created/w/wid-created",
            raw={"id": "did-created"},
        )

    async def publish_step(self, req, user_id=None):
        assert req.request_id == "req-1"
        assert user_id == "user-1"
        return OnshapePublishResponse(
            request_id=req.request_id,
            status="DONE",
            onshape_url="https://cad.onshape.com/documents/did-1/w/wid-1/e/eid-1",
            document_id="did-1",
            workspace_id="wid-1",
            element_id="eid-1",
            translation_id="tid-1",
            document_name="Demo",
            step_filename="result.step",
            raw={"ok": True},
        )

    async def get_translation(self, translation_id):
        assert translation_id == "tid-1"
        return {"requestState": "DONE", "resultElementIds": ["eid-1"]}

    async def get_links(self, request_id, user_id=None):
        assert request_id == "req-1"
        assert user_id == "user-1"
        return [{"request_id": request_id, "status": "DONE", "onshape_url": "https://cad.onshape.com/documents/did-1"}]

    async def refresh_latest_link(self, request_id, user_id=None):
        assert request_id == "req-1"
        assert user_id == "user-1"
        return {"request_id": request_id, "status": "DONE", "onshape_url": "https://cad.onshape.com/documents/did-1"}


def fake_service_factory():
    return FakeOnshapeService()


def build_session():
    registry = LayeredToolRegistry()
    registry.register_many([registration.to_definition() for registration in tool_onshape.build_onshape_tools(fake_service_factory)])
    session = SessionToolPool("session-a", registry)
    session.enable_plugin(registry.list_tools(layer="business"))
    return session


def onshape_context(**overrides):
    values = {
        "session_id": "session-a",
        "user_id": "user-1",
        "auth_principal": "principal-1",
        "role": "admin",
        "confirmed": True,
    }
    values.update(overrides)
    return ToolContext(**values)


def test_onshape_tools_register_as_business_plugin():
    tools = [registration.to_definition() for registration in tool_onshape.build_onshape_tools(fake_service_factory)]

    assert tool_onshape.PLUGIN_META.name == "onshape"
    assert tool_onshape.PLUGIN_META.layer == "business"
    assert tool_onshape.PLUGIN_META.safety["has_write_tools"] is True
    assert tool_onshape.PLUGIN_META.auth["requires_generated_file_ownership_for_publish"] is True
    assert len(tools) == 8
    assert {tool.layer for tool in tools} == {"business"}
    assert {tool.plugin_name for tool in tools} == {"onshape"}
    assert {tool.name for tool in tools} == {
        "onshape_list_documents",
        "onshape_list_elements",
        "onshape_list_partstudio_features",
        "onshape_create_document",
        "onshape_publish_step",
        "onshape_get_translation",
        "onshape_get_links",
        "onshape_refresh_latest_link",
    }


def test_onshape_business_tools_are_session_scoped_and_admin_gated():
    session_a = build_session()
    session_b = SessionToolPool("session-b", session_a.global_registry)

    user_names = {tool.name for tool in session_a.list_tools(onshape_context(role="user"))}
    admin_names = {tool.name for tool in session_a.list_tools(onshape_context(role="admin"))}
    other_session_names = {tool.name for tool in session_b.list_tools(onshape_context(session_id="session-b", role="admin"))}

    assert "onshape_list_documents" not in user_names
    assert "onshape_get_links" in user_names
    assert "onshape_publish_step" in user_names
    assert "onshape_list_documents" in admin_names
    assert "onshape_publish_step" in admin_names
    assert other_session_names == set()


@pytest.mark.asyncio
async def test_onshape_list_documents_returns_business_summary():
    result = await ToolExecutor(build_session()).execute(
        "onshape_list_documents",
        {"q": "demo", "limit": 10},
        onshape_context(),
    )

    assert result.status == "success"
    assert result.layer == "business"
    assert result.plugin_name == "onshape"
    assert result.summary["count"] == 1
    assert result.summary["documents"][0]["document_id"] == "did-1"
    assert result.summary["documents"][0]["default_workspace_id"] == "wid-1"


@pytest.mark.asyncio
async def test_onshape_create_document_requires_confirmation():
    result = await ToolExecutor(build_session()).execute(
        "onshape_create_document",
        {"name": "Created Doc"},
        onshape_context(confirmed=False),
    )

    assert result.status == "consent_required"
    assert result.error_code == "confirmation_required"
    assert result.needs_confirmation is True
    assert result.confirmation is not None
    assert result.confirmation.risk_level == "high"
    assert result.confirmation.preview_fields == ["name", "description", "is_public"]
    assert result.layer == "business"
    assert result.plugin_name == "onshape"


@pytest.mark.asyncio
async def test_onshape_create_document_returns_business_summary():
    result = await ToolExecutor(build_session()).execute(
        "onshape_create_document",
        {"name": "Created Doc"},
        onshape_context(),
    )

    assert result.status == "success"
    assert result.summary["document_id"] == "did-created"
    assert result.summary["default_workspace_id"] == "wid-created"


@pytest.mark.asyncio
async def test_onshape_publish_step_checks_generated_file_owner(monkeypatch):
    monkeypatch.setattr(tool_onshape, "request_belongs_to", lambda request_id, principal: request_id == "req-1" and principal == "principal-1")

    result = await ToolExecutor(build_session()).execute(
        "onshape_publish_step",
        {"request_id": "req-1", "document_name": "Demo"},
        onshape_context(role="user"),
    )

    assert result.status == "success"
    assert result.summary["translation_id"] == "tid-1"
    assert result.summary["onshape_url"].endswith("/e/eid-1")


@pytest.mark.asyncio
async def test_onshape_publish_step_rejects_wrong_generated_file_owner(monkeypatch):
    monkeypatch.setattr(tool_onshape, "request_belongs_to", lambda _request_id, _principal: False)

    result = await ToolExecutor(build_session()).execute(
        "onshape_publish_step",
        {"request_id": "req-1", "document_name": "Demo"},
        onshape_context(),
    )

    assert result.status == "permission_required"
    assert result.error_code == "permission_denied"
    assert result.error_type == "PermissionError"


@pytest.mark.asyncio
async def test_onshape_publish_step_requires_admin_for_existing_document(monkeypatch):
    monkeypatch.setattr(tool_onshape, "request_belongs_to", lambda _request_id, _principal: True)

    result = await ToolExecutor(build_session()).execute(
        "onshape_publish_step",
        {"request_id": "req-1", "document_id": "did-1", "workspace_id": "wid-1"},
        onshape_context(role="user"),
    )

    assert result.status == "permission_required"
    assert result.error_code == "permission_denied"
    assert result.error_type == "PermissionError"


@pytest.mark.asyncio
async def test_onshape_owned_link_tools_return_business_summaries():
    executor = ToolExecutor(build_session())

    links = await executor.execute("onshape_get_links", {"request_id": "req-1"}, onshape_context())
    refreshed = await executor.execute("onshape_refresh_latest_link", {"request_id": "req-1"}, onshape_context())

    assert links.status == "success"
    assert links.summary["count"] == 1
    assert links.layer == "business"
    assert refreshed.status == "success"
    assert refreshed.summary["status"] == "DONE"


@pytest.mark.asyncio
async def test_onshape_get_translation_checks_recorded_link(monkeypatch):
    async def fake_get_link(translation_id, user_id=None):
        assert translation_id == "tid-1"
        assert user_id == "user-1"
        return {"translation_id": translation_id, "user_id": user_id}

    monkeypatch.setattr(tool_onshape.history, "get_onshape_link_by_translation", fake_get_link)

    result = await ToolExecutor(build_session()).execute(
        "onshape_get_translation",
        {"translation_id": "tid-1"},
        onshape_context(),
    )

    assert result.status == "success"
    assert result.summary["status"] == "DONE"
    assert result.summary["element_ids"] == ["eid-1"]
