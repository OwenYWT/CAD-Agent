from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping
from time import monotonic, perf_counter
from typing import Any

from pydantic import ValidationError

from app.tools.models import ToolContext, ToolDefinition, ToolExecutionResult
from app.tools.registry import SessionToolPool


ToolAuditHandler = Callable[
    [ToolExecutionResult, dict[str, Any], ToolContext],
    Awaitable[None] | None,
]


class ToolExecutor:
    def __init__(
        self,
        pool: SessionToolPool,
        *,
        audit_handler: ToolAuditHandler | None = None,
    ):
        self.pool = pool
        self.audit_handler = audit_handler
        self._rate_windows: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | str | None = None,
        context: ToolContext | None = None,
    ) -> ToolExecutionResult:
        started = perf_counter()
        active_context = context or ToolContext(session_id=self.pool.session_id)
        tool = self.pool.get(name, active_context)
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
                layer=tool.layer if tool else None,
                plugin_name=tool.plugin_name if tool else None,
            )
            return await self._complete(result, {}, active_context)

        if tool is None:
            result = ToolExecutionResult(
                tool_name=name,
                status="permission_required",
                error_type="ToolUnavailable",
                error_message=f"Tool is not available for this session: {name}",
                duration_ms=self._duration_ms(started),
            )
            return await self._complete(result, raw_arguments, active_context)

        try:
            args = tool.args_model.model_validate(raw_arguments)
        except ValidationError as exc:
            return await self._complete(
                self._failure(tool, started, "ValidationError", str(exc)),
                raw_arguments,
                active_context,
            )

        if self._requires_confirmation(tool) and not active_context.confirmed:
            message = f"Tool '{tool.name}' requires confirmation before execution."
            result = ToolExecutionResult(
                tool_name=tool.name,
                status="consent_required",
                safety_level=tool.safety_level,
                error_message=message,
                needs_confirmation=True,
                confirmation_message=message,
                duration_ms=self._duration_ms(started),
                layer=tool.layer,
                plugin_name=tool.plugin_name,
            )
            return await self._complete(result, raw_arguments, active_context)

        if self._is_rate_limited(tool, active_context):
            result = ToolExecutionResult(
                tool_name=tool.name,
                status="rate_limited",
                safety_level=tool.safety_level,
                error_type="RateLimitExceeded",
                error_message=f"Tool '{tool.name}' exceeded its configured rate limit.",
                duration_ms=self._duration_ms(started),
                layer=tool.layer,
                plugin_name=tool.plugin_name,
            )
            return await self._complete(result, raw_arguments, active_context)

        try:
            response = await asyncio.wait_for(
                tool.handler(args, active_context),
                timeout=tool.timeout_s,
            )
        except asyncio.TimeoutError:
            result = self._failure(
                tool,
                started,
                "TimeoutError",
                f"Tool timed out after {tool.timeout_s:g}s",
            )
        except PermissionError as exc:
            result = ToolExecutionResult(
                tool_name=tool.name,
                status="permission_required",
                safety_level=tool.safety_level,
                error_type="PermissionError",
                error_message=str(exc),
                duration_ms=self._duration_ms(started),
                layer=tool.layer,
                plugin_name=tool.plugin_name,
            )
        except Exception as exc:
            result = self._failure(tool, started, type(exc).__name__, str(exc))
        else:
            if isinstance(response, ToolExecutionResult):
                result = response.model_copy(update={
                    "duration_ms": response.duration_ms or self._duration_ms(started),
                    "safety_level": tool.safety_level,
                    "layer": response.layer or tool.layer,
                    "plugin_name": response.plugin_name or tool.plugin_name,
                })
            elif isinstance(response, Mapping):
                result = ToolExecutionResult(
                    tool_name=tool.name,
                    status="success",
                    safety_level=tool.safety_level,
                    summary=dict(response),
                    raw=response,
                    duration_ms=self._duration_ms(started),
                    layer=tool.layer,
                    plugin_name=tool.plugin_name,
                )
            else:
                result = self._failure(
                    tool,
                    started,
                    "InvalidToolResult",
                    "Tool handlers must return a mapping or ToolExecutionResult.",
                )
        return await self._complete(result, raw_arguments, active_context)

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
    def _requires_confirmation(tool: ToolDefinition) -> bool:
        return tool.requires_confirmation or tool.safety_level in {"write", "destructive"}

    def _is_rate_limited(self, tool: ToolDefinition, context: ToolContext) -> bool:
        if not tool.rate_limit:
            return False
        key = (context.session_id or self.pool.session_id, tool.name)
        window = self._rate_windows[key]
        now = monotonic()
        while window and now - window[0] >= tool.rate_limit.period_s:
            window.popleft()
        if len(window) >= tool.rate_limit.count:
            return True
        window.append(now)
        return False

    @staticmethod
    def _duration_ms(started: float) -> int:
        return int((perf_counter() - started) * 1000)

    def _failure(
        self,
        tool: ToolDefinition,
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
            layer=tool.layer,
            plugin_name=tool.plugin_name,
        )

    async def _complete(
        self,
        result: ToolExecutionResult,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolExecutionResult:
        if self.audit_handler is not None:
            maybe_awaitable = self.audit_handler(result, arguments, context)
            if maybe_awaitable is not None:
                await maybe_awaitable
        return result
