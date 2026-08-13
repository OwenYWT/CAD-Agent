import asyncio

import pytest
from pydantic import BaseModel, Field

from app.tools import PluginLoader, SessionToolPool, ToolContext, ToolExecutor
from app.tools.models import RateLimitSpec, ToolRegistration


@pytest.fixture
def loader():
    plugin_loader = PluginLoader()
    plugin_loader.load_module("app.tools.plugins.basic.tool_time_math", expected_layer="basic")
    plugin_loader.load_module("app.tools.plugins.capability.tool_text_ops", expected_layer="capability")
    plugin_loader.load_module("app.tools.plugins.business.tool_admin_ops", expected_layer="business")
    return plugin_loader


def test_loader_imports_single_module_with_multiple_tools(loader):
    names = {tool.name for tool in loader.registry.list_tools()}

    assert "basic_time_now" in names
    assert "basic_math_add" in names
    assert "capability_text_slugify" in names
    assert "business_admin_echo" in names


def test_basic_tools_are_global_but_dynamic_tools_are_session_scoped(loader):
    session_a = SessionToolPool("session-a", loader.registry)
    session_b = SessionToolPool("session-b", loader.registry)
    capability_tools = loader.registry.list_tools(layer="capability")

    session_a.enable_plugin(capability_tools)

    context_a = ToolContext(session_id="session-a", role="user")
    context_b = ToolContext(session_id="session-b", role="user")
    assert {tool.name for tool in session_a.list_tools(context_a)} == {
        "basic_time_now",
        "basic_math_add",
        "capability_text_slugify",
    }
    assert {tool.name for tool in session_b.list_tools(context_b)} == {"basic_time_now", "basic_math_add"}


def test_business_tools_require_admin_visibility(loader):
    session = SessionToolPool("session-a", loader.registry)
    session.enable_plugin(loader.registry.list_tools(layer="business"))

    user_context = ToolContext(session_id="session-a", role="user")
    admin_context = ToolContext(session_id="session-a", role="admin")

    assert "business_admin_echo" not in {tool.name for tool in session.list_tools(user_context)}
    assert "business_admin_echo" in {tool.name for tool in session.list_tools(admin_context)}


@pytest.mark.asyncio
async def test_executor_runs_enabled_session_tool(loader):
    session = SessionToolPool("session-a", loader.registry)
    session.enable_plugin(loader.registry.list_tools(layer="capability"))
    executor = ToolExecutor(session)

    result = await executor.execute(
        "capability_text_slugify",
        {"text": "Hello CAD Agent"},
        ToolContext(session_id="session-a", role="user"),
    )

    assert result.status == "success"
    assert result.summary == {"slug": "hello-cad-agent"}
    assert result.layer == "capability"
    assert result.plugin_name == "text_ops"


@pytest.mark.asyncio
async def test_executor_blocks_unavailable_tool_for_session(loader):
    session = SessionToolPool("session-b", loader.registry)
    executor = ToolExecutor(session)

    result = await executor.execute(
        "capability_text_slugify",
        {"text": "Hello"},
        ToolContext(session_id="session-b", role="user"),
    )

    assert result.status == "permission_required"
    assert result.error_type == "ToolUnavailable"


@pytest.mark.asyncio
async def test_executor_requires_confirmation_for_business_tool(loader):
    session = SessionToolPool("session-a", loader.registry)
    session.enable_plugin(loader.registry.list_tools(layer="business"))
    executor = ToolExecutor(session)

    result = await executor.execute(
        "business_admin_echo",
        {"message": "deploy"},
        ToolContext(session_id="session-a", user_id="admin-1", role="admin", confirmed=False),
    )

    assert result.status == "consent_required"
    assert result.needs_confirmation is True


@pytest.mark.asyncio
async def test_executor_runs_confirmed_business_tool(loader):
    session = SessionToolPool("session-a", loader.registry)
    session.enable_plugin(loader.registry.list_tools(layer="business"))
    executor = ToolExecutor(session)

    result = await executor.execute(
        "business_admin_echo",
        {"message": "deploy"},
        ToolContext(session_id="session-a", user_id="admin-1", role="admin", confirmed=True),
    )

    assert result.status == "success"
    assert result.summary == {"message": "deploy", "approved_by": "admin-1"}


@pytest.mark.asyncio
async def test_executor_validates_structured_arguments(loader):
    session = SessionToolPool("session-a", loader.registry)
    session.enable_plugin(loader.registry.list_tools(layer="capability"))
    executor = ToolExecutor(session)

    result = await executor.execute(
        "capability_text_slugify",
        {"text": ""},
        ToolContext(session_id="session-a", role="user"),
    )

    assert result.status == "failure"
    assert result.error_type == "ValidationError"


class SleepArgs(BaseModel):
    delay_s: float = Field(..., ge=0)


async def sleep_tool(args: BaseModel, _context: ToolContext) -> dict:
    typed = SleepArgs.model_validate(args)
    await asyncio.sleep(typed.delay_s)
    return {"slept": typed.delay_s}


@pytest.mark.asyncio
async def test_executor_applies_timeout():
    loader = PluginLoader()
    tool = ToolRegistration(
        func=sleep_tool,
        name="capability_sleep",
        description="Sleep for a duration.",
        args_model=SleepArgs,
        layer="capability",
        plugin_name="sleep_ops",
        timeout_s=0.01,
    ).to_definition()
    loader.registry.register(tool)
    session = SessionToolPool("session-a", loader.registry)
    session.enable_tool(tool)

    result = await ToolExecutor(session).execute(
        "capability_sleep",
        {"delay_s": 0.05},
        ToolContext(session_id="session-a", role="user"),
    )

    assert result.status == "failure"
    assert result.error_type == "TimeoutError"


@pytest.mark.asyncio
async def test_executor_applies_session_tool_rate_limit():
    loader = PluginLoader()
    tool = ToolRegistration(
        func=sleep_tool,
        name="capability_limited_sleep",
        description="Rate limited sleep.",
        args_model=SleepArgs,
        layer="capability",
        plugin_name="sleep_ops",
        rate_limit=RateLimitSpec(count=1, period_s=60),
    ).to_definition()
    loader.registry.register(tool)
    session = SessionToolPool("session-a", loader.registry)
    session.enable_tool(tool)
    executor = ToolExecutor(session)
    context = ToolContext(session_id="session-a", role="user")

    first = await executor.execute("capability_limited_sleep", {"delay_s": 0}, context)
    second = await executor.execute("capability_limited_sleep", {"delay_s": 0}, context)

    assert first.status == "success"
    assert second.status == "rate_limited"
