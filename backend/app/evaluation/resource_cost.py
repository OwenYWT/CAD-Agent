"""Summarize measured execution resources from a finalized live report."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any


_MODEL_ACTIVITIES = {
    "agent_v2.requirements",
    "agent_v2.plan",
    "agent_v2.generate_source",
    "agent_v2.repair_source",
    "agent_v2.generate_operations",
    "agent_v2.repair_operations",
    "agent_v2.judge_visual",
    "agent_v2.repair_visual",
}


def _calls(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = report.get("tool_calls")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _non_negative_int(value: Any) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    return int(value)


def _wall_clock_ms(report: Mapping[str, Any]) -> int | None:
    started_at = report.get("started_at")
    finished_at = report.get("finished_at")
    if not isinstance(started_at, str) or not isinstance(finished_at, str):
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    elapsed = round((finished - started).total_seconds() * 1000)
    return max(0, elapsed)


def _stage(name: str) -> str:
    if name == "agent_v2.requirements":
        return "requirements"
    if name in {"agent_v2.decompose", "agent_v2.plan"}:
        return "planning"
    if name in {
        "agent_v2.validate_geometry",
        "agent_v2.render_visual",
        "agent_v2.judge_visual",
        "agent_v2.repair_visual",
        "agent_v2.validate_dfm",
        "agent_v2.generate_bom",
    }:
        return "validation"
    if name in {
        "agent_v2.allocate_candidate",
        "agent_v2.terminate_candidate",
        "agent_v2.seal_candidate",
    }:
        return "candidate"
    if name.startswith("agent_v2."):
        return "modeling"
    return "other"


def _ratio(measured: int, total: int) -> float:
    return round(measured / total, 4) if total else 0.0


def _call_summary(calls: list[Mapping[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    failed = 0
    retried = 0
    for call in calls:
        kind = str(call.get("call_kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if str(call.get("status") or "").lower() in {
            "error",
            "fail",
            "failed",
            "rejected",
        }:
            failed += 1
        attempt = _non_negative_int(call.get("attempt")) or 1
        if bool(call.get("replayed")) or attempt > 1:
            retried += 1
    return {
        "status": "measured",
        "total": len(calls),
        "by_kind": dict(sorted(by_kind.items())),
        "failed": failed,
        "retried": retried,
    }


def _duration_summary(
    report: Mapping[str, Any], calls: list[Mapping[str, Any]]
) -> dict[str, Any]:
    activities = [call for call in calls if call.get("call_kind") == "workflow_activity"]
    measured = []
    by_stage: dict[str, int] = {}
    for call in activities:
        duration = _non_negative_int(call.get("duration_ms"))
        if duration is None:
            continue
        measured.append(duration)
        stage = _stage(str(call.get("name") or ""))
        by_stage[stage] = by_stage.get(stage, 0) + duration
    wall_clock = _wall_clock_ms(report)
    if not measured and wall_clock is None:
        return {
            "status": "unavailable",
            "reason": "duration_evidence_unavailable",
            "measured_activity_calls": 0,
            "activity_calls": len(activities),
            "coverage_ratio": _ratio(0, len(activities)),
        }
    fully_measured = len(measured) == len(activities) and wall_clock is not None
    result: dict[str, Any] = {
        "status": "measured" if fully_measured else "partial",
        "activity_duration_ms": sum(measured),
        "by_stage_ms": dict(sorted(by_stage.items())),
        "measured_activity_calls": len(measured),
        "activity_calls": len(activities),
        "coverage_ratio": _ratio(len(measured), len(activities)),
    }
    if wall_clock is not None:
        result["wall_clock_ms"] = wall_clock
    return result


def _repair_summary(calls: list[Mapping[str, Any]]) -> dict[str, Any]:
    repairs = [
        call
        for call in calls
        if str(call.get("name") or "").startswith("agent_v2.repair_")
        and call.get("call_kind") == "workflow_activity"
    ]
    if not repairs:
        return {"status": "not_applicable", "call_count": 0, "duration_ms": None}
    durations = [
        duration
        for duration in (_non_negative_int(call.get("duration_ms")) for call in repairs)
        if duration is not None
    ]
    return {
        "status": "measured" if len(durations) == len(repairs) else "partial",
        "call_count": len(repairs),
        "duration_ms": sum(durations) if durations else None,
    }


def _model_usage_summary(calls: list[Mapping[str, Any]]) -> dict[str, Any]:
    model_calls = [
        call
        for call in calls
        if call.get("call_kind") == "workflow_activity"
        and str(call.get("name") or "") in _MODEL_ACTIVITIES
    ]
    measured = [
        call["model_usage"]
        for call in model_calls
        if isinstance(call.get("model_usage"), Mapping)
        and call["model_usage"].get("unit") == "token"
    ]
    if not measured:
        return {
            "status": "unavailable",
            "unit": "token",
            "reason": "model_usage_evidence_unavailable",
            "measured_calls": 0,
            "model_activity_calls": len(model_calls),
            "coverage_ratio": _ratio(0, len(model_calls)),
        }
    result: dict[str, Any] = {
        "status": "measured" if len(measured) == len(model_calls) else "partial",
        "unit": "token",
    }
    for key in ("input", "output", "total", "reasoning"):
        values = [
            value
            for value in (_non_negative_int(item.get(key)) for item in measured)
            if value is not None
        ]
        if values:
            result[key] = sum(values)
    result.update(
        {
            "measured_calls": len(measured),
            "model_activity_calls": len(model_calls),
            "coverage_ratio": _ratio(len(measured), len(model_calls)),
        }
    )
    return result


def build_resource_cost(report: Mapping[str, Any]) -> dict[str, Any]:
    """Build a truthful resource summary without estimating missing prices or usage."""

    calls = _calls(report)
    return {
        "status": "partial",
        "calls": _call_summary(calls),
        "duration": _duration_summary(report, calls),
        "repair": _repair_summary(calls),
        "model_usage": _model_usage_summary(calls),
        "monetary_cost": {
            "status": "unavailable",
            "reason": "pricing_evidence_unavailable",
        },
    }
