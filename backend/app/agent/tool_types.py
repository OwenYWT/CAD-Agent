from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field


ToolSafetyLevel = Literal["read", "import_export", "write", "destructive"]
ToolResultStatus = Literal["success", "failure", "permission_required", "consent_required"]


class ToolExecutionContext(BaseModel):
    request_id: str | None = None
    session_id: str | None = None
    panel_id: str | None = None
    user_id: str | None = None
    auth_principal: str | None = None
    allow_shared_onshape: bool = False
    confirmed: bool = False


class ToolExecutionResult(BaseModel):
    tool_name: str
    status: ToolResultStatus
    safety_level: ToolSafetyLevel
    summary: dict[str, Any] = Field(default_factory=dict)
    raw: Any = None
    error_type: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    needs_confirmation: bool = False
    confirmation_message: str | None = None


ToolHandler = Callable[[BaseModel, ToolExecutionContext], Awaitable[ToolExecutionResult | Mapping[str, Any]]]


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    args_model: type[BaseModel]
    safety_level: ToolSafetyLevel
    handler: ToolHandler
    requires_confirmation: bool = False
    timeout_s: float = 30.0

    def openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }
