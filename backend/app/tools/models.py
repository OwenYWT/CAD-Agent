from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


PluginLayer = Literal["basic", "capability", "business"]
ToolSafetyLevel = Literal["read", "compute", "import_export", "write", "destructive"]
ToolStatus = Literal["success", "failure", "permission_required", "consent_required", "rate_limited", "disabled"]
ToolVisibility = Literal["public", "user", "admin"]
ToolErrorCode = Literal[
    "tool_unavailable",
    "validation_error",
    "permission_denied",
    "confirmation_required",
    "rate_limit_exceeded",
    "timeout",
    "execution_error",
    "disabled",
]
ConfirmationRiskLevel = Literal["low", "medium", "high", "critical"]


class ToolContext(BaseModel):
    session_id: str | None = Field(None, min_length=1, max_length=128)
    user_id: str | None = Field(None, min_length=1, max_length=128)
    auth_principal: str | None = None
    role: str | None = Field(None, min_length=1, max_length=64)
    scopes: set[str] = Field(default_factory=set)
    allow_shared_onshape: bool = False
    enabled_plugins: set[str] = Field(default_factory=set)
    enabled_tools: set[str] = Field(default_factory=set)
    disabled_tools: set[str] = Field(default_factory=set)
    confirmed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class RateLimitSpec(BaseModel):
    count: int = Field(..., ge=1)
    period_s: float = Field(..., gt=0)


class ConfirmationPolicy(BaseModel):
    required: bool = True
    risk_level: ConfirmationRiskLevel = "medium"
    title: str = Field("Tool execution requires confirmation", min_length=1, max_length=200)
    message: str = Field("Please confirm before running this tool.", min_length=1, max_length=1000)
    confirm_label: str = Field("Confirm", min_length=1, max_length=64)
    preview_fields: list[str] = Field(default_factory=list)

    def public_metadata(self) -> dict[str, Any]:
        return self.model_dump()


class ToolExecutionResult(BaseModel):
    tool_name: str
    status: ToolStatus
    summary: dict[str, Any] = Field(default_factory=dict)
    raw: Any = None
    error_code: ToolErrorCode | None = None
    error_type: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    needs_confirmation: bool = False
    confirmation: ConfirmationPolicy | None = None
    layer: PluginLayer | None = None
    plugin_name: str | None = None


ToolHandler = Callable[[BaseModel, ToolContext], Awaitable[ToolExecutionResult | Mapping[str, Any]]]


class PluginMetadata(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z][a-zA-Z0-9_\-]*$")
    layer: PluginLayer
    version: str = Field("0.1.0", min_length=1, max_length=64)
    description: str = Field(..., min_length=1, max_length=1000)
    module_path: str | None = Field(None, min_length=1, max_length=256)
    use_cases: list[str] = Field(default_factory=list)
    tags: set[str] = Field(default_factory=set)
    auth: dict[str, Any] = Field(default_factory=dict)
    safety: dict[str, Any] = Field(default_factory=dict)
    default_enabled: bool = False
    disabled: bool = False

    @field_validator("tags")
    @classmethod
    def _tags_include_layer_and_plugin(cls, value: set[str], info):
        data = info.data
        layer = data.get("layer")
        name = data.get("name")
        normalized = {str(tag).strip() for tag in value if str(tag).strip()}
        if layer:
            normalized.add(layer)
        if name:
            normalized.add(name)
        return normalized

    def public_metadata(self, *, tool_count: int | None = None) -> dict[str, Any]:
        metadata = {
            "name": self.name,
            "layer": self.layer,
            "version": self.version,
            "description": self.description,
            "module_path": self.module_path,
            "use_cases": self.use_cases,
            "tags": sorted(self.tags),
            "auth": self.auth,
            "safety": self.safety,
            "default_enabled": self.default_enabled,
            "disabled": self.disabled,
        }
        if tool_count is not None:
            metadata["tool_count"] = tool_count
        return metadata


class ToolDefinition(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z][a-zA-Z0-9_\-]*$")
    description: str = Field(..., min_length=1, max_length=1000)
    args_model: type[BaseModel]
    handler: ToolHandler
    layer: PluginLayer
    plugin_name: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z][a-zA-Z0-9_\-]*$")
    version: str = Field("0.1.0", min_length=1, max_length=64)
    tags: set[str] = Field(default_factory=set)
    safety_level: ToolSafetyLevel = "read"
    visibility: ToolVisibility = "user"
    required_roles: set[str] = Field(default_factory=set)
    required_scopes: set[str] = Field(default_factory=set)
    required_session_scopes: set[str] = Field(default_factory=set)
    enabled_by_default: bool = False
    disabled: bool = False
    requires_confirmation: bool = False
    confirmation_policy: ConfirmationPolicy | None = None
    timeout_s: float = Field(30.0, gt=0, le=600)
    rate_limit: RateLimitSpec | None = None

    model_config = {"arbitrary_types_allowed": True}

    @field_validator("tags")
    @classmethod
    def _tags_include_layer_and_plugin(cls, value: set[str], info):
        data = info.data
        layer = data.get("layer")
        plugin_name = data.get("plugin_name")
        normalized = {str(tag).strip() for tag in value if str(tag).strip()}
        if layer:
            normalized.add(layer)
        if plugin_name:
            normalized.add(plugin_name)
        return normalized

    def openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }

    def public_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "layer": self.layer,
            "plugin_name": self.plugin_name,
            "version": self.version,
            "tags": sorted(self.tags),
            "safety_level": self.safety_level,
            "visibility": self.visibility,
            "required_roles": sorted(self.required_roles),
            "required_scopes": sorted(self.required_scopes),
            "required_session_scopes": sorted(self.required_session_scopes),
            "enabled_by_default": self.enabled_by_default,
            "disabled": self.disabled,
            "requires_confirmation": self.requires_confirmation,
            "confirmation_policy": self.effective_confirmation_policy().public_metadata()
            if self.effective_confirmation_policy()
            else None,
            "timeout_s": self.timeout_s,
            "rate_limit": self.rate_limit.model_dump() if self.rate_limit else None,
        }

    def effective_confirmation_policy(self) -> ConfirmationPolicy | None:
        if self.confirmation_policy:
            if self.requires_confirmation and not self.confirmation_policy.required:
                return self.confirmation_policy.model_copy(update={"required": True})
            return self.confirmation_policy
        if self.requires_confirmation:
            return ConfirmationPolicy()
        return None


class ToolRegistration(BaseModel):
    func: ToolHandler
    name: str
    description: str
    args_model: type[BaseModel]
    layer: PluginLayer
    plugin_name: str
    version: str = "0.1.0"
    tags: set[str] = Field(default_factory=set)
    safety_level: ToolSafetyLevel = "read"
    visibility: ToolVisibility = "user"
    required_roles: set[str] = Field(default_factory=set)
    required_scopes: set[str] = Field(default_factory=set)
    required_session_scopes: set[str] = Field(default_factory=set)
    enabled_by_default: bool = False
    disabled: bool = False
    requires_confirmation: bool = False
    confirmation_policy: ConfirmationPolicy | None = None
    timeout_s: float = Field(30.0, gt=0, le=600)
    rate_limit: RateLimitSpec | None = None

    model_config = {"arbitrary_types_allowed": True}

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            args_model=self.args_model,
            handler=self.func,
            layer=self.layer,
            plugin_name=self.plugin_name,
            version=self.version,
            tags=self.tags,
            safety_level=self.safety_level,
            visibility=self.visibility,
            required_roles=self.required_roles,
            required_scopes=self.required_scopes,
            required_session_scopes=self.required_session_scopes,
            enabled_by_default=self.enabled_by_default,
            disabled=self.disabled,
            requires_confirmation=self.requires_confirmation,
            confirmation_policy=self.confirmation_policy,
            timeout_s=self.timeout_s,
            rate_limit=self.rate_limit,
        )
