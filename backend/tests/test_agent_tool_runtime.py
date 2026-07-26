import pytest
from pydantic import BaseModel, Field

from app.agent.tool_executor import ToolExecutor
from app.agent.tool_registry import ToolRegistry
from app.agent.tool_types import AgentTool, ToolExecutionContext, ToolExecutionResult


class EchoArgs(BaseModel):
    text: str = Field(..., min_length=1)


async def echo_handler(args: BaseModel, _context: ToolExecutionContext) -> ToolExecutionResult:
    typed = EchoArgs.model_validate(args)
    return ToolExecutionResult(
        tool_name="echo",
        status="success",
        safety_level="read",
        summary={"text": typed.text},
    )


def test_tool_registry_exports_openai_tool_schema():
    registry = ToolRegistry([
        AgentTool(
            name="echo",
            description="Echo input text.",
            args_model=EchoArgs,
            safety_level="read",
            handler=echo_handler,
        )
    ])

    schema = registry.to_openai_tools()

    assert schema[0]["type"] == "function"
    assert schema[0]["function"]["name"] == "echo"
    assert schema[0]["function"]["parameters"]["properties"]["text"]["type"] == "string"


@pytest.mark.asyncio
async def test_tool_executor_runs_read_tool():
    registry = ToolRegistry([
        AgentTool(
            name="echo",
            description="Echo input text.",
            args_model=EchoArgs,
            safety_level="read",
            handler=echo_handler,
        )
    ])

    result = await ToolExecutor(registry).execute("echo", {"text": "hello"})

    assert result.status == "success"
    assert result.summary == {"text": "hello"}


@pytest.mark.asyncio
async def test_tool_executor_calls_audit_handler():
    audit_events = []
    registry = ToolRegistry([
        AgentTool(
            name="echo",
            description="Echo input text.",
            args_model=EchoArgs,
            safety_level="read",
            handler=echo_handler,
        )
    ])

    async def audit_handler(result, arguments, context):
        audit_events.append((result.tool_name, result.status, arguments, context.request_id))

    result = await ToolExecutor(registry, audit_handler=audit_handler).execute(
        "echo",
        {"text": "hello"},
        ToolExecutionContext(request_id="req-1"),
    )

    assert result.status == "success"
    assert audit_events == [("echo", "success", {"text": "hello"}, "req-1")]


@pytest.mark.asyncio
async def test_tool_executor_blocks_write_tool_without_confirmation():
    async def should_not_run(args: BaseModel, _context: ToolExecutionContext) -> ToolExecutionResult:
        raise AssertionError("write tool should require confirmation before running")

    registry = ToolRegistry([
        AgentTool(
            name="write_doc",
            description="Write something externally.",
            args_model=EchoArgs,
            safety_level="write",
            handler=should_not_run,
        )
    ])

    result = await ToolExecutor(registry).execute("write_doc", {"text": "hello"})

    assert result.status == "consent_required"
    assert result.needs_confirmation is True


@pytest.mark.asyncio
async def test_tool_executor_runs_confirmed_write_tool():
    registry = ToolRegistry([
        AgentTool(
            name="write_doc",
            description="Write something externally.",
            args_model=EchoArgs,
            safety_level="write",
            handler=echo_handler,
        )
    ])

    result = await ToolExecutor(registry).execute(
        "write_doc",
        {"text": "hello"},
        ToolExecutionContext(confirmed=True),
    )

    assert result.status == "success"
    assert result.summary == {"text": "hello"}


@pytest.mark.asyncio
async def test_tool_executor_accepts_json_string_arguments():
    registry = ToolRegistry([
        AgentTool(
            name="echo",
            description="Echo input text.",
            args_model=EchoArgs,
            safety_level="read",
            handler=echo_handler,
        )
    ])

    result = await ToolExecutor(registry).execute("echo", '{"text":"hello"}')

    assert result.status == "success"
    assert result.summary == {"text": "hello"}


@pytest.mark.asyncio
async def test_tool_executor_reports_permission_errors():
    async def needs_permission(args: BaseModel, _context: ToolExecutionContext) -> ToolExecutionResult:
        raise PermissionError("not allowed")

    registry = ToolRegistry([
        AgentTool(
            name="secure_read",
            description="Read secure data.",
            args_model=EchoArgs,
            safety_level="read",
            handler=needs_permission,
        )
    ])

    result = await ToolExecutor(registry).execute("secure_read", {"text": "hello"})

    assert result.status == "permission_required"
    assert result.error_type == "PermissionError"


@pytest.mark.asyncio
async def test_tool_executor_reports_validation_errors():
    registry = ToolRegistry([
        AgentTool(
            name="echo",
            description="Echo input text.",
            args_model=EchoArgs,
            safety_level="read",
            handler=echo_handler,
        )
    ])

    result = await ToolExecutor(registry).execute("echo", {"text": ""})

    assert result.status == "failure"
    assert result.error_type == "ValidationError"
