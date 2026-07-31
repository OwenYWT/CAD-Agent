import pytest

from app.agent.onshape_tools import build_onshape_tool_registry
from app.agent.tool_executor import ToolExecutor
from app.agent.tool_types import ToolExecutionContext
from app.models.schemas import OnshapeDocumentResponse, OnshapeDocumentsResponse, OnshapePublishResponse


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


def onshape_context(**overrides):
    values = {"allow_shared_onshape": True, "user_id": "user-1", "confirmed": True}
    values.update(overrides)
    return ToolExecutionContext(**values)


@pytest.mark.asyncio
async def test_onshape_list_documents_tool_returns_agent_summary():
    executor = ToolExecutor(build_onshape_tool_registry(fake_service_factory))

    result = await executor.execute("onshape_list_documents", {"q": "demo", "limit": 10}, onshape_context())

    assert result.status == "success"
    assert result.summary["count"] == 1
    assert result.summary["documents"][0]["document_id"] == "did-1"
    assert result.summary["documents"][0]["default_workspace_id"] == "wid-1"


@pytest.mark.asyncio
async def test_onshape_list_elements_tool_returns_agent_summary():
    executor = ToolExecutor(build_onshape_tool_registry(fake_service_factory))

    result = await executor.execute(
        "onshape_list_elements",
        {"document_id": "did-1", "workspace_id": "wid-1"},
        onshape_context(),
    )

    assert result.status == "success"
    assert result.summary["count"] == 1
    assert result.summary["elements"][0]["element_id"] == "eid-1"


@pytest.mark.asyncio
async def test_onshape_list_partstudio_features_tool_returns_agent_summary():
    executor = ToolExecutor(build_onshape_tool_registry(fake_service_factory))

    result = await executor.execute(
        "onshape_list_partstudio_features",
        {"document_id": "did-1", "workspace_id": "wid-1", "element_id": "eid-1"},
        onshape_context(),
    )

    assert result.status == "success"
    assert result.summary["count"] == 1
    assert result.summary["features"][0]["name"] == "Extrude 1"


@pytest.mark.asyncio
async def test_onshape_shared_document_tools_require_permission_context():
    executor = ToolExecutor(build_onshape_tool_registry(fake_service_factory))

    result = await executor.execute("onshape_list_documents", {"q": "demo", "limit": 10})

    assert result.status == "permission_required"


@pytest.mark.asyncio
async def test_onshape_create_document_tool_requires_confirmation():
    executor = ToolExecutor(build_onshape_tool_registry(fake_service_factory))

    result = await executor.execute(
        "onshape_create_document",
        {"name": "Created Doc"},
        onshape_context(confirmed=False),
    )

    assert result.status == "consent_required"


@pytest.mark.asyncio
async def test_onshape_create_document_tool_returns_agent_summary():
    executor = ToolExecutor(build_onshape_tool_registry(fake_service_factory))

    result = await executor.execute(
        "onshape_create_document",
        {"name": "Created Doc"},
        onshape_context(),
    )

    assert result.status == "success"
    assert result.summary["document_id"] == "did-created"
    assert result.summary["default_workspace_id"] == "wid-created"


@pytest.mark.asyncio
async def test_onshape_publish_step_tool_returns_agent_summary():
    executor = ToolExecutor(build_onshape_tool_registry(fake_service_factory))

    result = await executor.execute(
        "onshape_publish_step",
        {"request_id": "req-1", "document_name": "Demo"},
        onshape_context(auth_principal=None),
    )

    assert result.status == "success"
    assert result.summary["translation_id"] == "tid-1"
    assert result.summary["onshape_url"].endswith("/e/eid-1")


@pytest.mark.asyncio
async def test_onshape_links_tools_return_agent_summaries():
    executor = ToolExecutor(build_onshape_tool_registry(fake_service_factory))

    links = await executor.execute("onshape_get_links", {"request_id": "req-1"}, onshape_context())
    refreshed = await executor.execute("onshape_refresh_latest_link", {"request_id": "req-1"}, onshape_context())

    assert links.status == "success"
    assert links.summary["count"] == 1
    assert refreshed.status == "success"
    assert refreshed.summary["status"] == "DONE"
