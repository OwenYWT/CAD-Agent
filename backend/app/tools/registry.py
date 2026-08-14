from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.tools.models import PluginLayer, PluginMetadata, ToolContext, ToolDefinition


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
        self._plugins: dict[tuple[PluginLayer, str], PluginMetadata] = {}

    def register_plugin(self, metadata: PluginMetadata, *, replace: bool = False) -> None:
        key = (metadata.layer, metadata.name)
        if key in self._plugins and not replace:
            raise ValueError(f"Plugin already registered: {metadata.layer}/{metadata.name}")
        self._plugins[key] = metadata

    def get_plugin(self, name: str, *, layer: PluginLayer | None = None) -> PluginMetadata | None:
        if layer:
            return self._plugins.get((layer, name))
        matches = [metadata for (_layer, plugin_name), metadata in self._plugins.items() if plugin_name == name]
        if len(matches) > 1:
            raise ValueError(f"Plugin name is ambiguous across layers: {name}")
        return matches[0] if matches else None

    def set_plugin_disabled(self, name: str, *, disabled: bool = True, layer: PluginLayer | None = None) -> PluginMetadata:
        metadata = self.get_plugin(name, layer=layer)
        if not metadata:
            raise ValueError(f"Plugin is not registered: {name}")
        updated = metadata.model_copy(update={"disabled": disabled})
        self._plugins[(updated.layer, updated.name)] = updated
        return updated

    def list_plugins(self, *, layer: PluginLayer | None = None) -> list[PluginMetadata]:
        plugins = [
            metadata
            for (plugin_layer, _name), metadata in self._plugins.items()
            if layer is None or plugin_layer == layer
        ]
        return sorted(plugins, key=lambda metadata: (metadata.layer, metadata.name))

    def plugin_catalog(self, *, layer: PluginLayer | None = None) -> list[dict]:
        return [
            metadata.public_metadata(tool_count=len(self.list_tools_for_plugin(metadata.name, layer=metadata.layer)))
            for metadata in self.list_plugins(layer=layer)
        ]

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

    def list_tools_for_plugin(self, plugin_name: str, *, layer: PluginLayer | None = None) -> list[ToolDefinition]:
        if layer:
            return self._registry_for_layer(layer).filter_by_plugin(plugin_name)
        return [tool for tool in self.list_tools() if tool.plugin_name == plugin_name]

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
    disabled_plugins: set[str] = field(default_factory=set)

    def enable_tool(self, tool: ToolDefinition, *, replace: bool = False) -> None:
        if tool.layer == "basic":
            raise ValueError("Basic tools are managed by the global static registry")
        if tool.name in self.dynamic_tools and not replace:
            raise ValueError(f"Tool already enabled for session: {tool.name}")
        self.dynamic_tools[tool.name] = tool
        self.disabled_tools.discard(tool.name)
        self.disabled_plugins.discard(tool.plugin_name)

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
            self.disabled_plugins.discard(tool.plugin_name)

    def enable_plugin_by_name(
        self,
        plugin_name: str,
        *,
        layer: PluginLayer | None = None,
        replace: bool = False,
    ) -> list[str]:
        metadata = self.global_registry.get_plugin(plugin_name, layer=layer)
        if not metadata:
            raise ValueError(f"Plugin is not registered: {plugin_name}")
        if metadata.disabled:
            raise ValueError(f"Plugin is disabled: {metadata.layer}/{metadata.name}")
        tools = self.global_registry.list_tools_for_plugin(metadata.name, layer=metadata.layer)
        if not tools:
            raise ValueError(f"Plugin has no registered tools: {metadata.layer}/{metadata.name}")
        self.enable_plugin(tools, replace=replace)
        self.disabled_plugins.discard(metadata.name)
        return [tool.name for tool in tools]

    def disable_tool(self, name: str) -> None:
        if name in self.dynamic_tools:
            self.disabled_tools.add(name)

    def disable_plugin(self, plugin_name: str) -> list[str]:
        disabled = [name for name, tool in self.dynamic_tools.items() if tool.plugin_name == plugin_name]
        self.disabled_tools.update(disabled)
        if disabled:
            self.disabled_plugins.add(plugin_name)
        return disabled

    def unload_plugin(self, plugin_name: str) -> list[str]:
        removed = [name for name, tool in self.dynamic_tools.items() if tool.plugin_name == plugin_name]
        for name in removed:
            self.dynamic_tools.pop(name, None)
            self.disabled_tools.discard(name)
        self.disabled_plugins.discard(plugin_name)
        return removed

    def get(self, name: str, context: ToolContext | None = None) -> ToolDefinition | None:
        tool = self.dynamic_tools.get(name) or self.global_registry.basic.get(name)
        if not tool or name in self.disabled_tools or tool.plugin_name in self.disabled_plugins:
            return None
        if context and not self.is_visible(tool, context):
            return None
        return tool

    def list_tools(self, context: ToolContext | None = None) -> list[ToolDefinition]:
        tools = self.global_registry.static_basic_tools() + [
            tool
            for name, tool in self.dynamic_tools.items()
            if name not in self.disabled_tools and tool.plugin_name not in self.disabled_plugins and not tool.disabled
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
        if tool.required_scopes and not tool.required_scopes.issubset(context.scopes):
            return False
        if tool.required_session_scopes and not tool.required_session_scopes.issubset(context.enabled_plugins):
            return False
        if tool.name in context.disabled_tools:
            return False
        if context.enabled_tools and tool.name not in context.enabled_tools and tool.layer != "basic":
            return False
        return True
