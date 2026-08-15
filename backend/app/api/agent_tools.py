import hashlib
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.auth import rate_limiter, verify_api_key
from app.config import settings
from app.db import tenant_transaction
from app.principal_context import current_principal
from app.repositories.audit import append_audit_record
from app.storage import auth as auth_store
from app.tools import (
    PluginLoader,
    SessionToolPool,
    ToolContext,
    ToolExecutionResult,
    ToolExecutor,
)

router = APIRouter(prefix="/api/agent/tools", tags=["agent-tools"])


async def _audit_agent_tool(
    result: ToolExecutionResult,
    arguments: dict[str, Any],
    context: ToolContext,
) -> None:
    if not settings.durable_control_plane_enabled:
        return
    principal = current_principal()
    async with tenant_transaction(
        principal.tenant_id,
        principal.principal_id,
    ) as connection:
        await append_audit_record(
            connection,
            tenant_id=principal.tenant_id,
            actor_principal_id=principal.principal_id,
            action=f"agent_tool.{result.status}",
            target_type="agent_tool",
            target_id=result.tool_name,
            payload={
                "arguments": arguments,
                "request_id": context.request_id,
                "session_id": context.session_id,
                "panel_id": context.panel_id,
                "safety_level": result.safety_level,
                "error_type": result.error_type,
            },
        )


class AgentToolRuntime:
    """Authenticated API adapter for the standalone plugin framework."""

    def __init__(self):
        loader = PluginLoader()
        loader.load_module(
            "app.tools.plugins.business.tool_onshape",
            expected_layer="business",
        )
        self.pool = SessionToolPool("agent-tools-api", loader.registry)
        self.pool.enable_plugin(loader.registry.list_tools(layer="business"))
        self.executor = ToolExecutor(
            self.pool,
            audit_handler=_audit_agent_tool,
        )

    def list_agent_tools(
        self,
        context: ToolContext | None = None,
    ) -> list[dict[str, Any]]:
        active_context = context or ToolContext(
            session_id=self.pool.session_id,
            role="admin",
            allow_shared_onshape=True,
        )
        return [
            {
                **tool.public_metadata(),
                "parameters": tool.args_model.model_json_schema(),
            }
            for tool in self.pool.list_tools(active_context)
        ]

    def agent_tool_schemas(
        self,
        context: ToolContext | None = None,
    ) -> list[dict[str, Any]]:
        return self.pool.openai_tools(
            context
            or ToolContext(
                session_id=self.pool.session_id,
                role="admin",
                allow_shared_onshape=True,
            )
        )

    async def execute_agent_tool(
        self,
        name: str,
        arguments: dict[str, Any] | str,
        context: ToolContext,
    ):
        return await self.executor.execute(name, arguments, context)


@lru_cache(maxsize=1)
def _get_orchestrator() -> AgentToolRuntime:
    # Compatibility name for existing route/test call sites. This is the
    # standalone plugin runtime, never the process-local CAD orchestrator.
    return AgentToolRuntime()


class AgentToolExecuteRequest(BaseModel):
    arguments: dict[str, Any] | str = Field(default_factory=dict)
    confirmed: bool = False
    request_id: str | None = Field(None, min_length=1, max_length=128)
    session_id: str | None = Field(None, min_length=1, max_length=128)
    panel_id: str | None = Field(None, min_length=1, max_length=128)


def _owner_key(api_key: str | None) -> str | None:
    if not api_key:
        return None
    if api_key.startswith("user:"):
        return api_key.split(":", 1)[1]
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return f"api-key:{digest}"


async def _can_access_shared_onshape(api_key: str | None) -> bool:
    if not api_key or not api_key.startswith("user:"):
        return True
    user = await auth_store.get_user(api_key.split(":", 1)[1])
    return bool(user and user.get("is_admin"))


async def _tool_context(
    api_key: str | None,
    *,
    request_id: str | None = None,
    session_id: str | None = None,
    panel_id: str | None = None,
    confirmed: bool = False,
) -> ToolContext:
    shared_access = await _can_access_shared_onshape(api_key)
    return ToolContext(
        request_id=request_id,
        session_id=session_id or "agent-tools-api",
        panel_id=panel_id,
        user_id=_owner_key(api_key),
        auth_principal=api_key,
        role="admin" if shared_access else "user",
        allow_shared_onshape=shared_access,
        enabled_plugins={"onshape"},
        confirmed=confirmed,
    )


@router.get("")
async def list_agent_tools(
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    context = await _tool_context(api_key)
    runtime = _get_orchestrator()
    return {
        "tools": runtime.list_agent_tools(context),
        "openai_tools": runtime.agent_tool_schemas(context),
    }


@router.post("/{tool_name}/execute")
async def execute_agent_tool(
    tool_name: str,
    req: AgentToolExecuteRequest,
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    context = await _tool_context(
        api_key,
        request_id=req.request_id,
        session_id=req.session_id,
        panel_id=req.panel_id,
        confirmed=req.confirmed,
    )
    result = await _get_orchestrator().execute_agent_tool(
        tool_name,
        req.arguments,
        context,
    )
    return result.model_dump()
