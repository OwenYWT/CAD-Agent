from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.tools.models import PluginLayer, ToolContext, ToolDefinition


class ToolRegistry:
    def __init__(self, tools: Iterable[ToolDefinition] | None = None):
        self._tools: dict[str, ToolDefinition] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: ToolDefinition, *, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> ToolDefinition | None:
        return self._tools.pop(name, None)

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_tools(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def filter_by_plugin(self, plugin_name: str) -> list[ToolDefinition]:
        return [tool for tool in self._tools.values() if tool.plugin_name == plugin_name]

    def filter_by_tag(self, tag: str) -> list[ToolDefinition]:
        return [tool for tool in self._tools.values() if tag in tool.tags]


class LayeredToolRegistry:
    def __init__(self):
        self.basic = ToolRegistry()
        self.capability = ToolRegistry()
        self.business = ToolRegistry()

    def register(self, tool: ToolDefinition, *, replace: bool = False) -> None:
        self._registry_for_layer(tool.layer).register(tool, replace=replace)

    def register_many(self, tools: Iterable[ToolDefinition], *, replace: bool = False) -> None:
        for tool in tools:
            self.register(tool, replace=replace)

    def unregister(self, name: str) -> ToolDefinition | None:
        for registry in (self.basic, self.capability, self.business):
            removed = registry.unregister(name)
            if removed:
                return removed
        return None

    def get(self, name: str) -> ToolDefinition | None:
        for registry in (self.basic, self.capability, self.business):
            tool = registry.get(name)
            if tool:
                return tool
        return None

    def list_tools(self, *, layer: PluginLayer | None = None) -> list[ToolDefinition]:
        if layer:
            return self._registry_for_layer(layer).list_tools()
        return self.basic.list_tools() + self.capability.list_tools() + self.business.list_tools()

    def static_basic_tools(self) -> list[ToolDefinition]:
        return [tool for tool in self.basic.list_tools() if not tool.disabled]

    def _registry_for_layer(self, layer: PluginLayer) -> ToolRegistry:
        if layer == "basic":
            return self.basic
        if layer == "capability":
            return self.capability
        if layer == "business":
            return self.business
        raise ValueError(f"Unknown plugin layer: {layer}")


@dataclass
class SessionToolPool:
    session_id: str
    global_registry: LayeredToolRegistry
    dynamic_tools: dict[str, ToolDefinition] = field(default_factory=dict)
    disabled_tools: set[str] = field(default_factory=set)

    def enable_tool(self, tool: ToolDefinition, *, replace: bool = False) -> None:
        if tool.layer == "basic":
            raise ValueError("Basic tools are managed by the global static registry")
        if tool.name in self.dynamic_tools and not replace:
            raise ValueError(f"Tool already enabled for session: {tool.name}")
        self.dynamic_tools[tool.name] = tool
        self.disabled_tools.discard(tool.name)

    def enable_plugin(self, tools: Iterable[ToolDefinition], *, replace: bool = False) -> None:
        staged: dict[str, ToolDefinition] = {}
        for tool in tools:
            if tool.layer == "basic":
                raise ValueError("Basic tools are managed by the global static registry")
            if tool.name in self.dynamic_tools and not replace:
                raise ValueError(f"Tool already enabled for session: {tool.name}")
            staged[tool.name] = tool
        for name, tool in staged.items():
            self.dynamic_tools[name] = tool
            self.disabled_tools.discard(name)

    def disable_tool(self, name: str) -> None:
        if name in self.dynamic_tools:
            self.disabled_tools.add(name)

    def unload_plugin(self, plugin_name: str) -> list[str]:
        removed = [name for name, tool in self.dynamic_tools.items() if tool.plugin_name == plugin_name]
        for name in removed:
            self.dynamic_tools.pop(name, None)
            self.disabled_tools.discard(name)
        return removed

    def get(self, name: str, context: ToolContext | None = None) -> ToolDefinition | None:
        tool = self.dynamic_tools.get(name) or self.global_registry.basic.get(name)
        if not tool or name in self.disabled_tools:
            return None
        if context and not self.is_visible(tool, context):
            return None
        return tool

    def list_tools(self, context: ToolContext | None = None) -> list[ToolDefinition]:
        tools = self.global_registry.static_basic_tools() + [
            tool for name, tool in self.dynamic_tools.items() if name not in self.disabled_tools and not tool.disabled
        ]
        if context is None:
            return tools
        return [tool for tool in tools if self.is_visible(tool, context)]

    def openai_tools(self, context: ToolContext | None = None) -> list[dict]:
        return [tool.openai_tool_schema() for tool in self.list_tools(context)]

    @staticmethod
    def is_visible(tool: ToolDefinition, context: ToolContext) -> bool:
        if tool.disabled:
            return False
        if tool.visibility == "admin" and context.role != "admin":
            return False
        if tool.required_roles and (context.role not in tool.required_roles):
            return False
        if tool.required_session_scopes and not tool.required_session_scopes.issubset(context.enabled_plugins):
            return False
        if tool.name in context.disabled_tools:
            return False
        if context.enabled_tools and tool.name not in context.enabled_tools and tool.layer != "basic":
            return False
        return True
