from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


TERMINAL_STEP_TYPES = {"finalize_result"}
EXECUTE_STEP_TYPES = {"execute_cad_code", "execute_code", "executing_code"}
TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "cancelled"}


@dataclass(frozen=True)
class NextStepDecision:
    step_type: str | None
    reason: str
    input_data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def none(cls, reason: str) -> "NextStepDecision":
        return cls(step_type=None, reason=reason, input_data={})


def decide_next_step(steps: list[dict[str, Any]]) -> NextStepDecision:
    if not steps:
        return NextStepDecision.none("no_steps")

    latest = steps[-1]
    step_type = latest.get("step_type")
    status = latest.get("status")
    if status not in TERMINAL_STATUSES:
        return NextStepDecision.none("step_not_terminal")
    if step_type in TERMINAL_STEP_TYPES:
        return NextStepDecision.none("run_finalized")

    if step_type in EXECUTE_STEP_TYPES:
        if status == "succeeded":
            return _finalize_after_execute(latest)
        if status == "failed":
            return _repair_after_execute_failure(latest)
        return NextStepDecision.none("execute_terminal_without_route")

    if step_type == "repair_code":
        if status == "succeeded":
            return _execute_after_repair(latest)
        return _finalize_after_repair_failure(latest)

    return NextStepDecision.none("unsupported_latest_step")


def _repair_after_execute_failure(step: dict[str, Any]) -> NextStepDecision:
    input_data = step.get("input") or {}
    error = step.get("error") or {}
    attempt = int(input_data.get("attempt") or 1)
    return NextStepDecision(
        step_type="repair_code",
        reason="execute_failed",
        input_data={
            "attempt": attempt,
            "stage": "execution",
            "error_type": error.get("type") or "ExecutionError",
            "message": error.get("message") or "\u6267\u884c\u5931\u8d25",
        },
    )


def _execute_after_repair(step: dict[str, Any]) -> NextStepDecision:
    output = step.get("output") or {}
    if output.get("code_changed") is False:
        return NextStepDecision(
            step_type="finalize_result",
            reason="repair_no_change",
            input_data={"success": False, "message": "\u4fee\u590d\u540e\u4ee3\u7801\u672a\u53d1\u751f\u53d8\u5316"},
        )
    input_data = step.get("input") or {}
    attempt = int(input_data.get("attempt") or output.get("attempt") or 1) + 1
    next_input = {"attempt": attempt, "source": "repair_code"}
    if output.get("repaired_code") is not None:
        next_input["code"] = output["repaired_code"]
    for key in ("mode", "output_formats", "request_id", "user_prompt", "is_2d"):
        if key in input_data:
            next_input[key] = input_data[key]
    return NextStepDecision(
        step_type="execute_cad_code",
        reason="repair_succeeded",
        input_data=next_input,
    )


def _finalize_after_execute(step: dict[str, Any]) -> NextStepDecision:
    output = step.get("output") or {}
    input_data = step.get("input") or {}
    attempt = int(output.get("attempt") or input_data.get("attempt") or 1)
    return NextStepDecision(
        step_type="finalize_result",
        reason="execute_succeeded",
        input_data={"success": True, "attempt": attempt},
    )


def _finalize_after_repair_failure(step: dict[str, Any]) -> NextStepDecision:
    error = step.get("error") or {}
    return NextStepDecision(
        step_type="finalize_result",
        reason="repair_failed",
        input_data={"success": False, "message": error.get("message") or "\u81ea\u52a8\u4fee\u590d\u5931\u8d25"},
    )
