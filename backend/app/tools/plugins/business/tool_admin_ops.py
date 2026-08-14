from __future__ import annotations

from pydantic import BaseModel, Field

from app.tools.models import ToolContext, ToolRegistration


class AdminEchoArgs(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


async def admin_echo(args: BaseModel, context: ToolContext) -> dict:
    values = AdminEchoArgs.model_validate(args)
    if context.role != "admin" or not context.user_id:
        raise PermissionError("An authenticated admin is required.")
    return {"message": values.message, "approved_by": context.user_id}


PLUGIN_TOOLS = [
    ToolRegistration(
        func=admin_echo,
        name="business_admin_echo",
        description="Return an administrator-approved message for audited tool-flow verification.",
        args_model=AdminEchoArgs,
        layer="business",
        plugin_name="admin_ops",
        safety_level="write",
        visibility="admin",
        required_roles={"admin"},
        requires_confirmation=True,
    ),
]
