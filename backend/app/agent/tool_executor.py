from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from pydantic import ValidationError

from app.agent.tool_registry import ToolRegistry
from app.agent.tool_types import AgentTool, ToolExecutionContext, ToolExecutionResult


ToolAuditHandler = Callable[[ToolExecutionResult, dict[str, Any], ToolExecutionContext], Awaitable[None] | None]


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        audit_handler: ToolAuditHandler | None = None,
    ):
        self.registry = registry
        self.audit_handler = audit_handler

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | str | None = None,
        context: ToolExecutionContext | None = None,
    ) -> ToolExecutionResult:
        started = perf_counter()
        tool = self.registry.get(name)
        active_context = context or ToolExecutionContext()
        try:
            raw_arguments = self._normalize_arguments(arguments)
        except ValueError as exc:
            result = ToolExecutionResult(
                tool_name=name,
                status="failure",
                safety_level=tool.safety_level if tool else "read",
                error_type="ValidationError",
                error_message=str(exc),
                duration_ms=self._duration_ms(started),
            )
            await self._audit(result, {}, active_context)
            return result

        if tool is None:
            result = ToolExecutionResult(
                tool_name=name,
                status="failure",
                safety_level="read",
                error_type="UnknownTool",
                error_message=f"Unknown tool: {name}",
                duration_ms=self._duration_ms(started),
            )
            await self._audit(result, raw_arguments, active_context)
            return result

        try:
            args = tool.args_model.model_validate(raw_arguments)
        except ValidationError as exc:
            result = self._failure(tool, started, "ValidationError", str(exc))
            await self._audit(result, raw_arguments, active_context)
            return result

        if self._requires_confirmation(tool) and not active_context.confirmed:
            result = ToolExecutionResult(
                tool_name=tool.name,
                status="consent_required",
                safety_level=tool.safety_level,
                needs_confirmation=True,
                confirmation_message=f"Tool '{tool.name}' requires confirmation before execution.",
                duration_ms=self._duration_ms(started),
            )
            await self._audit(result, raw_arguments, active_context)
            return result

        try:
            response = await asyncio.wait_for(
                tool.handler(args, active_context),
                timeout=tool.timeout_s,
            )
        except asyncio.TimeoutError:
            result = self._failure(tool, started, "TimeoutError", f"Tool timed out after {tool.timeout_s:g}s")
            await self._audit(result, raw_arguments, active_context)
            return result
        except PermissionError as exc:
            result = ToolExecutionResult(
                tool_name=tool.name,
                status="permission_required",
                safety_level=tool.safety_level,
                error_type="PermissionError",
                error_message=str(exc),
                duration_ms=self._duration_ms(started),
            )
            await self._audit(result, raw_arguments, active_context)
            return result
        except Exception as exc:
            result = self._failure(tool, started, type(exc).__name__, str(exc))
            await self._audit(result, raw_arguments, active_context)
            return result

        if isinstance(response, ToolExecutionResult):
            result = response.model_copy(update={"duration_ms": response.duration_ms or self._duration_ms(started)})
        else:
            result = ToolExecutionResult(
                tool_name=tool.name,
                status="success",
                safety_level=tool.safety_level,
                summary=dict(response),
                raw=response,
                duration_ms=self._duration_ms(started),
            )
        await self._audit(result, raw_arguments, active_context)
        return result

    @staticmethod
    def _normalize_arguments(arguments: dict[str, Any] | str | None) -> dict[str, Any]:
        if arguments is None:
            return {}
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError("Tool arguments must be a JSON object") from exc
            if not isinstance(parsed, dict):
                raise ValueError("Tool arguments must be a JSON object")
            return parsed
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
        return dict(arguments)

    @staticmethod
    def _requires_confirmation(tool: AgentTool) -> bool:
        return tool.requires_confirmation or tool.safety_level in {"write", "destructive"}

    @staticmethod
    def _duration_ms(started: float) -> int:
        return int((perf_counter() - started) * 1000)

    def _failure(
        self,
        tool: AgentTool,
        started: float,
        error_type: str,
        error_message: str,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            tool_name=tool.name,
            status="failure",
            safety_level=tool.safety_level,
            error_type=error_type,
            error_message=error_message,
            duration_ms=self._duration_ms(started),
        )

    async def _audit(
        self,
        result: ToolExecutionResult,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> None:
        if self.audit_handler is None:
            return
        maybe_awaitable = self.audit_handler(result, arguments, context)
        if asyncio.iscoroutine(maybe_awaitable):
            await maybe_awaitable
