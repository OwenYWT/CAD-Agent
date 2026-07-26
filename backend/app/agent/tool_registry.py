from __future__ import annotations

from collections.abc import Iterable

from app.agent.tool_types import AgentTool, ToolSafetyLevel


class ToolRegistry:
    def __init__(self, tools: Iterable[AgentTool] | None = None):
        self._tools: dict[str, AgentTool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: AgentTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[AgentTool]:
        return list(self._tools.values())

    def to_openai_tools(
        self,
        *,
        safety_levels: set[ToolSafetyLevel] | None = None,
    ) -> list[dict]:
        return [
            tool.openai_tool_schema()
            for tool in self._tools.values()
            if safety_levels is None or tool.safety_level in safety_levels
        ]
