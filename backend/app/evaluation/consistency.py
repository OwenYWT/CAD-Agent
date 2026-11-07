"""Stable summaries for comparing real evaluation call sequences."""

from __future__ import annotations

import hashlib
import json
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

    return {
        "sequence_match": base_sequence == candidate_sequence,
        "structure_match": base_structural == candidate_structural,
        "base": summary(base_sequence),
        "candidate": summary(candidate_sequence),
        "differences": differences,
    }
