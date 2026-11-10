"""Derive structured repair gain from authoritative live evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _timeline(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = report.get("timeline")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _evidence_by_id(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    value = report.get("evidence")
    if not isinstance(value, list):
        return {}
    return {
        str(item["evidence_id"]): item
        for item in value
        if isinstance(item, Mapping) and item.get("evidence_id")
    }


def _repair_events(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        event
        for event in _timeline(report)
        if str((event.get("payload") or {}).get("activity") or "").startswith(
            "agent_v2.repair_"
        )
    ]


def _duration(events: list[Mapping[str, Any]]) -> int | None:
    values = [
        int((event.get("payload") or {}).get("duration_ms"))
        for event in events
        if isinstance((event.get("payload") or {}).get("duration_ms"), (int, float))
        and not isinstance((event.get("payload") or {}).get("duration_ms"), bool)
    ]
    return sum(max(0, value) for value in values) if values else None


def _result_status(event: Mapping[str, Any], observed: Mapping[str, Any]) -> str:
    outcome = str(event.get("status") or "").lower()
    if outcome in {"pass", "passed", "success", "succeeded"}:
        return "passed"
    if outcome in {"fail", "failed", "error", "rejected"}:
        return "failed"
    acceptance = observed.get("acceptance")
    checks = acceptance.get("evidence") if isinstance(acceptance, Mapping) else None
    outcomes = {
        str(check.get("outcome") or "").lower()
        for check in checks or ()
        if isinstance(check, Mapping)
    }
    if outcomes and outcomes <= {"passed", "pass", "success", "succeeded"}:
        return "passed"
    if "failed" in outcomes or "fail" in outcomes:
        return "failed"
    return "unknown"


def _measurements(observed: Mapping[str, Any]) -> list[dict[str, Any]]:
    acceptance = observed.get("acceptance")
    checks = acceptance.get("evidence") if isinstance(acceptance, Mapping) else None
    if not isinstance(checks, list):
        return []
    result = []
    for check in checks:
        if not isinstance(check, Mapping) or not check.get("check_id"):
            continue
        details = check.get("details")
        details = details if isinstance(details, Mapping) else {}
        comparison = details.get("comparison") or check.get("comparison")
        comparison = comparison if isinstance(comparison, Mapping) else {}
        measured = check.get("measured")
        if isinstance(measured, list):
            measured = measured[0] if measured else None
        target = comparison.get("value")
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (target, measured)
        ):
            continue
        target = float(target)
        measured = float(measured)
        result.append(
            {
                "check_id": str(check["check_id"]),
                "target": target,
                "measured": measured,
                "error": abs(target - measured),
                "unit": str(comparison.get("unit") or ""),
                "outcome": str(check.get("outcome") or "unknown"),
            }
        )
    return result


def _geometry_results(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence_by_id = _evidence_by_id(report)
    results = []
    for event in _timeline(report):
        if event.get("event_type") != "geometry_validation_finished":
            continue
        evidence_ids = event.get("evidence_ids")
        if not isinstance(evidence_ids, list):
            continue
        for evidence_id in evidence_ids:
            evidence = evidence_by_id.get(str(evidence_id))
            observed = evidence.get("observed") if evidence else None
            if not isinstance(observed, Mapping):
                continue
            results.append(
                {
                    "evidence_id": str(evidence_id),
                    "status": _result_status(event, observed),
                    "checks": _measurements(observed),
                }
            )
    return results


def build_repair_gain(report: Mapping[str, Any]) -> dict[str, Any]:
    repairs = _repair_events(report)
    duration = _duration(repairs)
    base = {"repair_count": len(repairs), "repair_duration_ms": duration}
    if not repairs:
        return {"status": "not_applicable", **base}

    geometry = _geometry_results(report)
    if len(geometry) < 2:
        return {
            "status": "unknown",
            **base,
            "reason": "comparable_geometry_evidence_unavailable",
        }

    before = geometry[0]
    after = geometry[-1]
    before_by_id = {item["check_id"]: item for item in before["checks"]}
    after_by_id = {item["check_id"]: item for item in after["checks"]}
    common_ids = [
        check_id
        for check_id in sorted(set(before_by_id) & set(after_by_id))
        if before_by_id[check_id]["target"] == after_by_id[check_id]["target"]
        and before_by_id[check_id]["unit"] == after_by_id[check_id]["unit"]
    ]
    if not common_ids:
        return {
            "status": "unknown",
            **base,
            "reason": "comparable_geometry_check_unavailable",
        }

    improved = []
    regressed = []
    remaining = []
    for check_id in common_ids:
        before_check = before_by_id[check_id]
        after_check = after_by_id[check_id]
        before_passed = before_check["outcome"].lower() in {
            "pass",
            "passed",
            "success",
            "succeeded",
        }
        after_passed = after_check["outcome"].lower() in {
            "pass",
            "passed",
            "success",
            "succeeded",
        }
        if after_check["error"] < before_check["error"] or (
            not before_passed and after_passed
        ):
            improved.append(check_id)
        elif after_check["error"] > before_check["error"]:
            regressed.append(check_id)
        if after_check["outcome"].lower() not in {"pass", "passed", "success", "succeeded"}:
            remaining.append(check_id)

    return {
        "status": "measured",
        **base,
        "before": before,
        "after": after,
        "improved_checks": improved,
        "regressed_checks": regressed,
        "remaining_failures": remaining,
    }
