import hashlib
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.agent.tool_types import ToolExecutionContext
from app.api.auth import rate_limiter, verify_api_key
from app.storage import auth as auth_store

router = APIRouter(prefix="/api/agent/tools", tags=["agent-tools"])


def _get_orchestrator():
    from app.api.websocket import _get_orchestrator as get_orchestrator

    return get_orchestrator()


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


@router.get("")
async def list_agent_tools(
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    orchestrator = _get_orchestrator()
    return {"tools": orchestrator.list_agent_tools(), "openai_tools": orchestrator.agent_tool_schemas()}


@router.post("/{tool_name}/execute")
async def execute_agent_tool(
    tool_name: str,
    req: AgentToolExecuteRequest,
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    await rate_limiter.check(request, api_key)
    context = ToolExecutionContext(
        request_id=req.request_id,
        session_id=req.session_id,
        panel_id=req.panel_id,
        user_id=_owner_key(api_key),
        auth_principal=api_key,
        allow_shared_onshape=await _can_access_shared_onshape(api_key),
        confirmed=req.confirmed,
    )
    result = await _get_orchestrator().execute_agent_tool(tool_name, req.arguments, context)
    return result.model_dump()
