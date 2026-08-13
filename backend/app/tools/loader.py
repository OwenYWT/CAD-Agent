from __future__ import annotations

import importlib
from collections.abc import Iterable
from types import ModuleType
from typing import Any

from app.tools.models import PluginLayer, ToolDefinition, ToolRegistration
from app.tools.registry import LayeredToolRegistry


class PluginLoadError(RuntimeError):
    pass


class PluginLoader:
    def __init__(self, registry: LayeredToolRegistry | None = None):
        self.registry = registry or LayeredToolRegistry()

    def load_module(self, module_path: str, *, expected_layer: PluginLayer | None = None, replace: bool = False) -> list[ToolDefinition]:
        module = importlib.import_module(module_path)
        tools = self.tools_from_module(module, expected_layer=expected_layer)
        self.registry.register_many(tools, replace=replace)
        return tools

    def load_modules(self, module_paths: Iterable[str], *, replace: bool = False) -> list[ToolDefinition]:
        loaded: list[ToolDefinition] = []
        for module_path in module_paths:
            loaded.extend(self.load_module(module_path, replace=replace))
        return loaded

    @staticmethod
    def tools_from_module(module: ModuleType, *, expected_layer: PluginLayer | None = None) -> list[ToolDefinition]:
        raw_tools = getattr(module, "PLUGIN_TOOLS", None)
        if raw_tools is None:
            raise PluginLoadError(f"Plugin module {module.__name__} does not define PLUGIN_TOOLS")
        if not isinstance(raw_tools, list):
            raise PluginLoadError(f"Plugin module {module.__name__} PLUGIN_TOOLS must be a list")
        definitions = [PluginLoader._coerce_tool(raw_tool, module.__name__) for raw_tool in raw_tools]
        if expected_layer:
            mismatched = [tool.name for tool in definitions if tool.layer != expected_layer]
            if mismatched:
                raise PluginLoadError(
                    f"Plugin module {module.__name__} exported tools outside {expected_layer}: {', '.join(mismatched)}"
                )
        return definitions

    @staticmethod
    def _coerce_tool(raw_tool: Any, module_name: str) -> ToolDefinition:
        if isinstance(raw_tool, ToolDefinition):
            return raw_tool
        if isinstance(raw_tool, ToolRegistration):
            return raw_tool.to_definition()
        if isinstance(raw_tool, dict):
            data = dict(raw_tool)
            if "handler" not in data and "func" in data:
                data["handler"] = data.pop("func")
            try:
                return ToolDefinition(**data)
            except Exception as exc:
                raise PluginLoadError(f"Invalid tool definition in {module_name}: {exc}") from exc
        raise PluginLoadError(f"Unsupported tool definition in {module_name}: {type(raw_tool).__name__}")
