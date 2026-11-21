"""Stable summaries for comparing real evaluation call sequences."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping
from typing import Any


_SEQUENCE_FIELDS = (
    "call_kind",
    "name",
    "status",
    "attempt",
    "replayed",
    "input_hash",
    "output_hash",
)


def normalize_tool_sequence(tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, list):
        return []
    normalized = []
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        normalized.append({
            key: call.get(key)
            for key in _SEQUENCE_FIELDS
        })
    return normalized


def _signature(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def summarize_tool_calls(tool_calls: Any) -> dict[str, Any]:
    sequence = normalize_tool_sequence(tool_calls)
    structural = [
        {
            "call_kind": call["call_kind"],
            "name": call["name"],
            "attempt": call["attempt"],
            "replayed": call["replayed"],
        }
        for call in sequence
    ]
    by_kind = dict(sorted(Counter(str(call["call_kind"]) for call in sequence).items()))
    by_name = dict(sorted(Counter(str(call["name"]) for call in sequence).items()))
    return {
        "normalized_sequence": sequence,
        "sequence_signature": _signature(sequence),
        "structural_signature": _signature(structural),
        "total_call_count": len(sequence),
        "by_kind": by_kind,
        "by_name": by_name,
    }


def _structural_sequence(sequence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "call_kind": call["call_kind"],
            "name": call["name"],
            "attempt": call["attempt"],
            "replayed": call["replayed"],
        }
        for call in sequence
    ]


def _identity(call: Mapping[str, Any]) -> tuple[str, str]:
    return str(call.get("call_kind")), str(call.get("name"))


def _descriptor(call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "call_kind": call.get("call_kind"),
        "name": call.get("name"),
        "attempt": call.get("attempt"),
        "replayed": call.get("replayed"),
        "status": call.get("status"),
    }


def _retry_count(sequence: list[dict[str, Any]]) -> int:
    total = 0
    for call in sequence:
        try:
            total += max(0, int(call.get("attempt") or 1) - 1)
        except (TypeError, ValueError):
            continue
    return total


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _values_equal(base_value: Any, candidate_value: Any) -> bool:
    if _is_number(base_value) and _is_number(candidate_value):
        return math.isclose(
            float(base_value),
            float(candidate_value),
            rel_tol=1e-9,
            abs_tol=1e-6,
        )
    if isinstance(base_value, list) and isinstance(candidate_value, list):
        return len(base_value) == len(candidate_value) and all(
            _values_equal(base_item, candidate_item)
            for base_item, candidate_item in zip(base_value, candidate_value)
        )
    return base_value == candidate_value


def _geometry_values(report: Mapping[str, Any]) -> dict[str, Any]:
    profile = report.get("complexity_profile")
    if not isinstance(profile, Mapping):
        return {}
    geometry = profile.get("geometry")
    if not isinstance(geometry, Mapping):
        return {}
    values: dict[str, Any] = {}
    dimensions = geometry.get("dimensions_mm")
    if isinstance(dimensions, list) and dimensions and all(_is_number(item) for item in dimensions):
        values["dimensions_mm"] = dimensions
    for key in ("solid_count", "face_count", "volume_mm3"):
        value = geometry.get(key)
        if _is_number(value):
            values[key] = value
    return values


def _compare_geometry(
    base_report: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
) -> str:
    base_values = _geometry_values(base_report)
    candidate_values = _geometry_values(candidate_report)
    shared_keys = sorted(set(base_values) & set(candidate_values))
    if not shared_keys:
        return "unknown"
    if any(
        not _values_equal(base_values[key], candidate_values[key])
        for key in shared_keys
    ):
        return "different"
    return "consistent"


_UNKNOWN_STATUS_VALUES = {"", "unknown", "unavailable", "not_recorded"}


def _quality_values(report: Mapping[str, Any]) -> dict[str, str]:
    matrix = report.get("quality_matrix")
    if not isinstance(matrix, Mapping):
        return {}
    values: dict[str, str] = {}
    for key in ("geometry", "dfm", "visual", "artifact"):
        entry = matrix.get(key)
        if not isinstance(entry, Mapping):
            continue
        status = str(entry.get("status") or "").strip().lower()
        if status not in _UNKNOWN_STATUS_VALUES:
            values[key] = status
    return values


def _compare_quality(
    base_report: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
) -> str:
    base_values = _quality_values(base_report)
    candidate_values = _quality_values(candidate_report)
    shared_keys = sorted(set(base_values) & set(candidate_values))
    if not shared_keys:
        return "unknown"
    if any(base_values[key] != candidate_values[key] for key in shared_keys):
        return "different"
    return "consistent"


def _lifecycle_value(report: Mapping[str, Any]) -> str | None:
    matrix = report.get("quality_matrix")
    if isinstance(matrix, Mapping):
        lifecycle = matrix.get("lifecycle")
        if isinstance(lifecycle, Mapping):
            status = str(lifecycle.get("status") or "").strip().lower()
            if status not in _UNKNOWN_STATUS_VALUES:
                return status
    timeline = report.get("timeline")
    if isinstance(timeline, list):
        for event in reversed(timeline):
            if not isinstance(event, Mapping) or event.get("event_type") != "session_finished":
                continue
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                continue
            status = str(payload.get("business_status") or "").strip().lower()
            if status not in _UNKNOWN_STATUS_VALUES:
                return status
    return None


def _compare_lifecycle(
    base_report: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
) -> str:
    base_value = _lifecycle_value(base_report)
    candidate_value = _lifecycle_value(candidate_report)
    if base_value is None or candidate_value is None:
        return "unknown"
    return "consistent" if base_value == candidate_value else "different"


def _tool_path_status(
    base_sequence: list[dict[str, Any]],
    candidate_sequence: list[dict[str, Any]],
    structure_match: bool,
    status_changes: list[dict[str, Any]],
) -> str:
    if not base_sequence and not candidate_sequence:
        return "unknown"
    if not base_sequence or not candidate_sequence:
        return "different"
    if structure_match and not status_changes:
        return "consistent"
    return "different"


def _comparison_conclusion(
    base_report: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
    *,
    base_sequence: list[dict[str, Any]],
    candidate_sequence: list[dict[str, Any]],
    structure_match: bool,
    status_changes: list[dict[str, Any]],
    evaluation_metrics: Mapping[str, Any],
) -> dict[str, str]:
    tool_path = _tool_path_status(
        base_sequence,
        candidate_sequence,
        structure_match,
        status_changes,
    )
    geometry = _compare_geometry(base_report, candidate_report)
    quality = _compare_quality(base_report, candidate_report)
    lifecycle = _compare_lifecycle(base_report, candidate_report)
    outcome_values = (geometry, quality, lifecycle)
    if "different" in outcome_values:
        outcome = "different"
    elif "unknown" in outcome_values:
        outcome = "unknown"
    else:
        outcome = "consistent"

    if tool_path == "different":
        status = "different"
        reason = "tool_path_changed"
    elif geometry == "different":
        status = "different"
        reason = "geometry_changed"
    elif quality == "different":
        status = "different"
        reason = "quality_changed"
    elif lifecycle == "different":
        status = "different"
        reason = "lifecycle_changed"
    elif tool_path == "unknown" and outcome == "unknown":
        status = "not_comparable"
        reason = "no_comparable_evidence"
    elif tool_path == "unknown" or outcome == "unknown":
        status = "unknown"
        reason = "incomplete_comparison_evidence"
    else:
        status = "consistent"
        reason = "tool_path_and_outcome_match"

    resource_status = str(evaluation_metrics.get("status") or "not_comparable")
    if resource_status not in {"comparable", "not_comparable"}:
        resource_status = "not_comparable"
    return {
        "status": status,
        "reason": reason,
        "tool_path": tool_path,
        "outcome": outcome,
        "geometry": geometry,
        "quality": quality,
        "lifecycle": lifecycle,
        "resource": resource_status,
    }


def _multiset_difference(
    base: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base_counts = Counter(_identity(call) for call in base)
    candidate_counts = Counter(_identity(call) for call in candidate)
    base_by_identity: dict[tuple[str, str], list[dict[str, Any]]] = {}
    candidate_by_identity: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for call in base:
        base_by_identity.setdefault(_identity(call), []).append(call)
    for call in candidate:
        candidate_by_identity.setdefault(_identity(call), []).append(call)

    removed: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    for identity in sorted(set(base_counts) | set(candidate_counts)):
        difference = base_counts[identity] - candidate_counts[identity]
        removed.extend(_descriptor(call) for call in base_by_identity.get(identity, [])[:difference])
        difference = candidate_counts[identity] - base_counts[identity]
        added.extend(_descriptor(call) for call in candidate_by_identity.get(identity, [])[:difference])
    return added, removed


def compare_evaluation_reports(
    base_report: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare two live reports without treating runtime duration as drift."""

    base_sequence = normalize_tool_sequence(base_report.get("tool_calls"))
    candidate_sequence = normalize_tool_sequence(candidate_report.get("tool_calls"))
    base_structural = _structural_sequence(base_sequence)
    candidate_structural = _structural_sequence(candidate_sequence)
    added, removed = _multiset_difference(base_sequence, candidate_sequence)
    shared_length = min(len(base_sequence), len(candidate_sequence))
    status_changes = []
    input_hash_changes = []
    output_hash_changes = []
    for index in range(shared_length):
        base_call = base_sequence[index]
        candidate_call = candidate_sequence[index]
        if base_call["status"] != candidate_call["status"]:
            status_changes.append(
                {
                    "index": index,
                    "name": candidate_call["name"],
                    "base": base_call["status"],
                    "candidate": candidate_call["status"],
                }
            )
        if base_call["input_hash"] != candidate_call["input_hash"]:
            input_hash_changes.append({"index": index, "name": candidate_call["name"]})
        if base_call["output_hash"] != candidate_call["output_hash"]:
            output_hash_changes.append({"index": index, "name": candidate_call["name"]})

    base_order = [_identity(call) for call in base_sequence]
    candidate_order = [_identity(call) for call in candidate_sequence]
    differences = {
        "added": added,
        "removed": removed,
        "reordered": (
            base_order != candidate_order
            and Counter(base_order) == Counter(candidate_order)
        ),
        "status_changes": status_changes,
        "input_hash_changes": input_hash_changes,
        "output_hash_changes": output_hash_changes,
    }

    def summary(sequence: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "sequence_signature": _signature(sequence),
            "structural_signature": _signature(_structural_sequence(sequence)),
            "total_call_count": len(sequence),
            "retry_count": _retry_count(sequence),
        }

    from .baseline import compare_live_report_metrics

    evaluation_metrics = compare_live_report_metrics(base_report, candidate_report)

    return {
        "sequence_match": base_sequence == candidate_sequence,
        "structure_match": base_structural == candidate_structural,
        "base": summary(base_sequence),
        "candidate": summary(candidate_sequence),
        "differences": differences,
        "evaluation_metrics": evaluation_metrics,
        "conclusion": _comparison_conclusion(
            base_report,
            candidate_report,
            base_sequence=base_sequence,
            candidate_sequence=candidate_sequence,
            structure_match=base_structural == candidate_structural,
            status_changes=status_changes,
            evaluation_metrics=evaluation_metrics,
        ),
    }
