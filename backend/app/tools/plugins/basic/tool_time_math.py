from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from app.tools.models import ToolContext, ToolRegistration


class TimeNowArgs(BaseModel):
    """Arguments for reading the control-plane clock."""


class MathAddArgs(BaseModel):
    left: float = Field(..., allow_inf_nan=False, description="Left operand.")
    right: float = Field(..., allow_inf_nan=False, description="Right operand.")


async def time_now(_args: BaseModel, _context: ToolContext) -> dict:
    return {"timezone": "UTC", "iso8601": datetime.now(UTC).isoformat()}


async def math_add(args: BaseModel, _context: ToolContext) -> dict:
    values = MathAddArgs.model_validate(args)
    return {"result": values.left + values.right}


PLUGIN_TOOLS = [
    ToolRegistration(
        func=time_now,
        name="basic_time_now",
        description="Read the current UTC time from the control plane.",
        args_model=TimeNowArgs,
        layer="basic",
        plugin_name="time_math",
        safety_level="read",
        visibility="public",
        enabled_by_default=True,
    ),
    ToolRegistration(
        func=math_add,
        name="basic_math_add",
        description="Add two finite numeric values.",
        args_model=MathAddArgs,
        layer="basic",
        plugin_name="time_math",
        safety_level="compute",
        visibility="public",
        enabled_by_default=True,
    ),
]
