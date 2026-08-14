from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from time import monotonic, perf_counter
from typing import Any

from pydantic import ValidationError

from app.tools.models import ToolContext, ToolDefinition, ToolExecutionResult
from app.tools.registry import SessionToolPool


class ToolExecutor:
    def __init__(self, pool: SessionToolPool):
        self.pool = pool
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
        if tool is None:
            return ToolExecutionResult(
                tool_name=name,
                status="permission_required",
                error_code="tool_unavailable",
                error_type="ToolUnavailable",
                error_message=f"Tool is not available for this session: {name}",
                duration_ms=self._duration_ms(started),
            )
        try:
            raw_arguments = self._normalize_arguments(arguments)
            args = tool.args_model.model_validate(raw_arguments)
        except (ValueError, ValidationError) as exc:
            return self._failure(tool, started, "ValidationError", str(exc), error_code="validation_error")

        confirmation_policy = tool.effective_confirmation_policy()
        if confirmation_policy and confirmation_policy.required and not active_context.confirmed:
            return ToolExecutionResult(
                tool_name=tool.name,
                status="consent_required",
                error_code="confirmation_required",
                error_message=f"Tool '{tool.name}' requires confirmation before execution.",
                needs_confirmation=True,
                confirmation=confirmation_policy,
                duration_ms=self._duration_ms(started),
                layer=tool.layer,
                plugin_name=tool.plugin_name,
            )

        if self._is_rate_limited(tool, active_context):
            return ToolExecutionResult(
                tool_name=tool.name,
                status="rate_limited",
                error_code="rate_limit_exceeded",
                error_type="RateLimitExceeded",
                error_message=f"Tool '{tool.name}' exceeded its configured rate limit.",
                duration_ms=self._duration_ms(started),
                layer=tool.layer,
                plugin_name=tool.plugin_name,
            )

        try:
            response = await asyncio.wait_for(tool.handler(args, active_context), timeout=tool.timeout_s)
        except asyncio.TimeoutError:
            return self._failure(
                tool,
                started,
                "TimeoutError",
                f"Tool timed out after {tool.timeout_s:g}s",
                error_code="timeout",
            )
        except PermissionError as exc:
            return ToolExecutionResult(
                tool_name=tool.name,
                status="permission_required",
                error_code="permission_denied",
                error_type="PermissionError",
                error_message=str(exc),
                duration_ms=self._duration_ms(started),
                layer=tool.layer,
                plugin_name=tool.plugin_name,
            )
        except Exception as exc:
            return self._failure(tool, started, type(exc).__name__, str(exc), error_code="execution_error")

        if isinstance(response, ToolExecutionResult):
            return response.model_copy(update={
                "duration_ms": response.duration_ms or self._duration_ms(started),
                "layer": response.layer or tool.layer,
                "plugin_name": response.plugin_name or tool.plugin_name,
            })
        return ToolExecutionResult(
            tool_name=tool.name,
            status="success",
            summary=dict(response),
            raw=response,
            duration_ms=self._duration_ms(started),
            layer=tool.layer,
            plugin_name=tool.plugin_name,
        )

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
        *,
        error_code: str = "execution_error",
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            tool_name=tool.name,
            status="failure",
            error_code=error_code,
            error_type=error_type,
            error_message=error_message,
            duration_ms=self._duration_ms(started),
            layer=tool.layer,
            plugin_name=tool.plugin_name,
        )
