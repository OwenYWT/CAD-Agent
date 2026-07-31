from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.tool_types import ToolExecutionResult
from app.api import agent_tools as agent_tools_api
from app.main import app as production_app


class FakeOrchestrator:
    def __init__(self):
        self.context = None
        self.arguments = None

    def list_agent_tools(self):
        return [
            {
                "name": "echo",
                "description": "Echo input text.",
                "safety_level": "read",
                "requires_confirmation": False,
                "parameters": {"type": "object"},
            }
        ]

    def agent_tool_schemas(self):
        return [{"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}}]

    async def execute_agent_tool(self, name, arguments, context):
        self.arguments = arguments
        self.context = context
        return ToolExecutionResult(
            tool_name=name,
            status="success",
            safety_level="read",
            summary={"arguments": arguments, "confirmed": context.confirmed},
        )


def create_test_app():
    app = FastAPI()
    app.include_router(agent_tools_api.router)
    return app


def test_agent_tools_router_is_registered_in_production_app():
    paths = {route.path for route in production_app.routes}
    assert "/api/agent/tools" in paths
    assert "/api/agent/tools/{tool_name}/execute" in paths


def test_production_agent_tool_runtime_is_real_and_gates_writes():
    runtime = agent_tools_api.AgentToolRuntime()

    names = {tool["name"] for tool in runtime.list_agent_tools()}

    assert "onshape_list_documents" in names
    assert "onshape_create_document" in names
    assert all(
        schema["function"]["name"] in names
        for schema in runtime.agent_tool_schemas()
    )


def test_agent_tools_api_lists_registered_tools(monkeypatch):
    fake = FakeOrchestrator()
    monkeypatch.setattr(agent_tools_api, "_get_orchestrator", lambda: fake)

    with TestClient(create_test_app()) as client:
        response = client.get("/api/agent/tools")

    assert response.status_code == 200
    body = response.json()
    assert body["tools"][0]["name"] == "echo"
    assert body["openai_tools"][0]["function"]["name"] == "echo"


def test_agent_tools_api_executes_with_request_context(monkeypatch):
    fake = FakeOrchestrator()
    monkeypatch.setattr(agent_tools_api, "_get_orchestrator", lambda: fake)

    with TestClient(create_test_app()) as client:
        response = client.post(
            "/api/agent/tools/echo/execute",
            json={
                "arguments": {"text": "hello"},
                "confirmed": True,
                "request_id": "req-1",
                "session_id": "session-1",
                "panel_id": "panel-1",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["summary"]["confirmed"] is True
    assert fake.context.request_id == "req-1"
    assert fake.context.session_id == "session-1"
    assert fake.context.panel_id == "panel-1"
    assert fake.context.allow_shared_onshape is True


def test_production_agent_tool_write_requires_confirmation(monkeypatch):
    monkeypatch.setattr(
        agent_tools_api,
        "_get_orchestrator",
        agent_tools_api.AgentToolRuntime,
    )

    with TestClient(create_test_app()) as client:
        response = client.post(
            "/api/agent/tools/onshape_create_document/execute",
            json={
                "arguments": {"name": "不会实际创建"},
                "confirmed": False,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "consent_required"
    assert body["needs_confirmation"] is True
