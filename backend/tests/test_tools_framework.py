import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from app.tools import PluginLoader, SessionToolPool, ToolContext, ToolExecutor
from app.tools.models import RateLimitSpec, ToolRegistration


@pytest.fixture
def loader():
    plugin_loader = PluginLoader()
    plugin_loader.load_module(
        "app.tools.plugins.basic.tool_time_math",
        expected_layer="basic",
    )
    plugin_loader.load_module(
        "app.tools.plugins.capability.tool_text_ops",
        expected_layer="capability",
    )
    plugin_loader.load_module(
        "app.tools.plugins.business.tool_admin_ops",
        expected_layer="business",
    )
    return plugin_loader


def test_loader_imports_single_module_with_multiple_tools(loader):
    names = {tool.name for tool in loader.registry.list_tools()}

    assert "basic_time_now" in names
    assert "basic_math_add" in names
    assert "capability_text_slugify" in names
    assert "business_admin_echo" in names


def test_loader_registers_plugin_metadata_for_discovery(loader):
    catalog = {
        plugin["name"]: plugin
        for plugin in loader.registry.plugin_catalog()
    }

    assert catalog["time_math"]["layer"] == "basic"
    assert catalog["time_math"]["default_enabled"] is True
    assert catalog["time_math"]["tool_count"] == 2
    assert catalog["text_ops"]["layer"] == "capability"
    assert catalog["text_ops"]["tool_count"] == 1
    assert catalog["admin_ops"]["layer"] == "business"
    assert catalog["admin_ops"]["safety"]["requires_confirmation"] is True


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
    assert {tool.name for tool in session_b.list_tools(context_b)} == {
        "basic_time_now",
        "basic_math_add",
    }


def test_business_tools_require_admin_visibility(loader):
    session = SessionToolPool("session-a", loader.registry)
    session.enable_plugin(loader.registry.list_tools(layer="business"))

    user_context = ToolContext(session_id="session-a", role="user")
    admin_context = ToolContext(session_id="session-a", role="admin")

    assert "business_admin_echo" not in {
        tool.name for tool in session.list_tools(user_context)
    }
    assert "business_admin_echo" in {
        tool.name for tool in session.list_tools(admin_context)
    }


def test_session_can_enable_and_disable_plugin_by_name(loader):
    session = SessionToolPool("session-a", loader.registry)

    enabled = session.enable_plugin_by_name("text_ops", layer="capability")
    assert enabled == ["capability_text_slugify"]
    assert "capability_text_slugify" in {
        tool.name
        for tool in session.list_tools(ToolContext(session_id="session-a"))
    }

    disabled = session.disable_plugin("text_ops")
    assert disabled == ["capability_text_slugify"]
    assert "capability_text_slugify" not in {
        tool.name
        for tool in session.list_tools(ToolContext(session_id="session-a"))
    }

    reenabled = session.enable_plugin_by_name(
        "text_ops",
        layer="capability",
        replace=True,
    )
    assert reenabled == ["capability_text_slugify"]
    assert "capability_text_slugify" in {
        tool.name
        for tool in session.list_tools(ToolContext(session_id="session-a"))
    }


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
    assert result.error_code == "tool_unavailable"
    assert result.error_type == "ToolUnavailable"


@pytest.mark.asyncio
async def test_executor_requires_confirmation_for_business_tool(loader):
    session = SessionToolPool("session-a", loader.registry)
    session.enable_plugin(loader.registry.list_tools(layer="business"))
    executor = ToolExecutor(session)

    result = await executor.execute(
        "business_admin_echo",
        {"message": "deploy"},
        ToolContext(
            session_id="session-a",
            user_id="admin-1",
            role="admin",
            confirmed=False,
        ),
    )

    assert result.status == "consent_required"
    assert result.error_code == "confirmation_required"
    assert result.needs_confirmation is True
    assert result.safety_level == "write"
    assert result.confirmation_message
    assert result.confirmation is not None
    assert result.confirmation.risk_level == "low"
    assert result.confirmation.preview_fields == ["message"]


@pytest.mark.asyncio
async def test_executor_runs_confirmed_business_tool(loader):
    session = SessionToolPool("session-a", loader.registry)
    session.enable_plugin(loader.registry.list_tools(layer="business"))
    executor = ToolExecutor(session)

    result = await executor.execute(
        "business_admin_echo",
        {"message": "deploy"},
        ToolContext(
            session_id="session-a",
            user_id="admin-1",
            role="admin",
            confirmed=True,
        ),
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
    assert result.error_code == "validation_error"
    assert result.error_type == "ValidationError"


class SleepArgs(BaseModel):
    delay_s: float = Field(..., ge=0)


async def sleep_tool(args: BaseModel, _context: ToolContext) -> dict:
    typed = SleepArgs.model_validate(args)
    await asyncio.sleep(typed.delay_s)
    return {"slept": typed.delay_s}


@pytest.mark.asyncio
async def test_executor_applies_timeout():
    plugin_loader = PluginLoader()
    tool = ToolRegistration(
        func=sleep_tool,
        name="capability_sleep",
        description="Sleep for a duration.",
        args_model=SleepArgs,
        layer="capability",
        plugin_name="sleep_ops",
        timeout_s=0.01,
    ).to_definition()
    plugin_loader.registry.register(tool)
    session = SessionToolPool("session-a", plugin_loader.registry)
    session.enable_tool(tool)

    result = await ToolExecutor(session).execute(
        "capability_sleep",
        {"delay_s": 0.05},
        ToolContext(session_id="session-a", role="user"),
    )

    assert result.status == "failure"
    assert result.error_code == "timeout"
    assert result.error_type == "TimeoutError"


@pytest.mark.asyncio
async def test_executor_applies_session_tool_rate_limit():
    plugin_loader = PluginLoader()
    tool = ToolRegistration(
        func=sleep_tool,
        name="capability_limited_sleep",
        description="Rate limited sleep.",
        args_model=SleepArgs,
        layer="capability",
        plugin_name="sleep_ops",
        rate_limit=RateLimitSpec(count=1, period_s=60),
    ).to_definition()
    plugin_loader.registry.register(tool)
    session = SessionToolPool("session-a", plugin_loader.registry)
    session.enable_tool(tool)
    executor = ToolExecutor(session)
    context = ToolContext(session_id="session-a", role="user")

    first = await executor.execute(
        "capability_limited_sleep",
        {"delay_s": 0},
        context,
    )
    second = await executor.execute(
        "capability_limited_sleep",
        {"delay_s": 0},
        context,
    )

    assert first.status == "success"
    assert second.status == "rate_limited"
    assert second.error_code == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_executor_requires_confirmation_for_write_safety_without_flag():
    plugin_loader = PluginLoader()
    tool = ToolRegistration(
        func=sleep_tool,
        name="capability_write_without_flag",
        description="Write-class tool with no redundant confirmation flag.",
        args_model=SleepArgs,
        layer="capability",
        plugin_name="write_ops",
        safety_level="write",
        requires_confirmation=False,
    ).to_definition()
    plugin_loader.registry.register(tool)
    session = SessionToolPool("session-a", plugin_loader.registry)
    session.enable_tool(tool)

    result = await ToolExecutor(session).execute(
        tool.name,
        {"delay_s": 0},
        ToolContext(session_id="session-a", role="user", confirmed=False),
    )

    assert result.status == "consent_required"
    assert result.error_code == "confirmation_required"
    assert result.needs_confirmation is True
    assert result.safety_level == "write"
    assert result.confirmation is not None


@pytest.mark.asyncio
async def test_context_scopes_gate_tool_visibility_and_execution():
    plugin_loader = PluginLoader()
    tool = ToolRegistration(
        func=sleep_tool,
        name="capability_scoped_sleep",
        description="Scoped sleep.",
        args_model=SleepArgs,
        layer="capability",
        plugin_name="sleep_ops",
        required_scopes={"tools:sleep"},
    ).to_definition()
    plugin_loader.registry.register(tool)
    session = SessionToolPool("session-a", plugin_loader.registry)
    session.enable_tool(tool)

    missing = await ToolExecutor(session).execute(
        "capability_scoped_sleep",
        {"delay_s": 0},
        ToolContext(session_id="session-a", role="user"),
    )
    allowed = await ToolExecutor(session).execute(
        "capability_scoped_sleep",
        {"delay_s": 0},
        ToolContext(
            session_id="session-a",
            role="user",
            scopes={"tools:sleep"},
        ),
    )

    assert missing.status == "permission_required"
    assert missing.error_code == "tool_unavailable"
    assert allowed.status == "success"


@pytest.mark.asyncio
async def test_executor_audits_every_result(loader):
    session = SessionToolPool("session-a", loader.registry)
    session.enable_plugin(loader.registry.list_tools(layer="capability"))
    records = []

    async def audit(result, arguments, context):
        records.append((result, arguments, context))

    result = await ToolExecutor(session, audit_handler=audit).execute(
        "capability_text_slugify",
        {"text": "Audit Me"},
        ToolContext(
            session_id="session-a",
            request_id="req-audit",
            role="user",
        ),
    )

    assert result.status == "success"
    assert len(records) == 1
    assert records[0][0].tool_name == "capability_text_slugify"
    assert records[0][1] == {"text": "Audit Me"}
    assert records[0][2].request_id == "req-audit"


def test_plugin_template_contains_required_exports():
    template = (
        Path(__file__).resolve().parents[1]
        / "app/tools/templates/plugin_module.py.template"
    )
    content = template.read_text(encoding="utf-8")

    assert "PLUGIN_META = PluginMetadata" in content
    assert "PLUGIN_TOOLS = [" in content
    assert "ToolRegistration(" in content
