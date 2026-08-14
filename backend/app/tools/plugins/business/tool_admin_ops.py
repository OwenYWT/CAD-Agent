from __future__ import annotations

from pydantic import BaseModel, Field

from app.tools.models import (
    ConfirmationPolicy,
    PluginMetadata,
    ToolContext,
    ToolRegistration,
)


PLUGIN_NAME = "admin_ops"
PLUGIN_VERSION = "0.1.0"
PLUGIN_META = PluginMetadata(
    name=PLUGIN_NAME,
    layer="business",
    version=PLUGIN_VERSION,
    description="Authenticated administrative tools for tool-runtime verification.",
    use_cases=["Verify an administrator-approved write tool flow."],
    tags={"admin", "verification"},
    auth={"required_roles": ["admin"]},
    safety={"has_write_tools": True, "requires_confirmation": True},
)


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
        plugin_name=PLUGIN_NAME,
        version=PLUGIN_VERSION,
        safety_level="write",
        visibility="admin",
        required_roles={"admin"},
        requires_confirmation=True,
        confirmation_policy=ConfirmationPolicy(
            risk_level="low",
            title="Confirm administrative echo",
            message="Confirm this audited administrative tool execution.",
            preview_fields=["message"],
        ),
    ),
]
