"""Compare measured quality, complexity, and resource fields between live reports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _source(report: Mapping[str, Any]) -> tuple[str | None, str | None]:
    source = report.get("source")
    if not isinstance(source, Mapping):
        return None, None
    kind = source.get("kind")
    source_id = source.get("id")
    return (
        str(kind) if kind is not None else None,
        str(source_id) if source_id is not None else None,
    )


def _numeric_delta(
    base: Mapping[str, Any],
    candidate: Mapping[str, Any],
    fields: Mapping[str, str],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for output_name, field_path in fields.items():
        base_value = base.get(field_path)
        candidate_value = candidate.get(field_path)
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (base_value, candidate_value)
        ):
            continue
        result[output_name] = {
            "base": base_value,
            "candidate": candidate_value,
            "delta": candidate_value - base_value,
        }
    return result


def _nested(report: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = report.get(key)
    return value if isinstance(value, Mapping) else {}


def _quality(base: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    base_matrix = _nested(base, "quality_matrix")
    candidate_matrix = _nested(candidate, "quality_matrix")
    result: dict[str, Any] = {}
    for key in ("geometry", "dfm", "visual", "artifact", "lifecycle"):
        base_value = _nested(base_matrix, key).get("status")
        candidate_value = _nested(candidate_matrix, key).get("status")
        if base_value is None or candidate_value is None:
            continue
        result[key] = {
            "base": str(base_value),
            "candidate": str(candidate_value),
            "changed": base_value != candidate_value,
        }
    return result


def _complexity(base: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    base_profile = _nested(base, "complexity_profile")
    candidate_profile = _nested(candidate, "complexity_profile")
    result: dict[str, Any] = {}
    base_geometry = _nested(base_profile, "geometry")
    candidate_geometry = _nested(candidate_profile, "geometry")
    base_dimensions = base_geometry.get("dimensions_mm")
    candidate_dimensions = candidate_geometry.get("dimensions_mm")
    if isinstance(base_dimensions, list) and isinstance(candidate_dimensions, list):
        result["geometry_dimensions_mm"] = {
            "base": base_dimensions,
            "candidate": candidate_dimensions,
            "changed": base_dimensions != candidate_dimensions,
        }
    result.update(
        _numeric_delta(
            base_geometry,
            candidate_geometry,
            {
                "solid_count": "solid_count",
                "face_count": "face_count",
                "volume_mm3": "volume_mm3",
            },
        )
    )
    base_operation = _nested(base_profile, "operation")
    candidate_operation = _nested(candidate_profile, "operation")
    result.update(
        _numeric_delta(
            base_operation,
            candidate_operation,
            {
                "cad_operation_count": "cad_operation_count",
                "workflow_activity_count": "workflow_activity_count",
                "repair_count": "repair_count",
                "candidate_count": "candidate_count",
            },
        )
    )
    return result


def _resource(base: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    base_cost = _nested(base, "resource_cost")
    candidate_cost = _nested(candidate, "resource_cost")
    result: dict[str, Any] = {}
    result.update(
        _numeric_delta(
            _nested(base_cost, "calls"),
            _nested(candidate_cost, "calls"),
            {
                "calls_total": "total",
                "calls_failed": "failed",
                "calls_retried": "retried",
            },
        )
    )
    result.update(
        _numeric_delta(
            _nested(base_cost, "duration"),
            _nested(candidate_cost, "duration"),
            {
                "wall_clock_ms": "wall_clock_ms",
                "activity_duration_ms": "activity_duration_ms",
            },
        )
    )
    result.update(
        _numeric_delta(
            _nested(base_cost, "model_usage"),
            _nested(candidate_cost, "model_usage"),
            {
                "model_input_tokens": "input",
                "model_output_tokens": "output",
                "model_total_tokens": "total",
            },
        )
    )
    result.update(
        _numeric_delta(
            _nested(base_cost, "repair"),
            _nested(candidate_cost, "repair"),
            {"repair_duration_ms": "duration_ms", "repair_call_count": "call_count"},
        )
    )
    return result


def compare_live_report_metrics(
    base_report: Mapping[str, Any],
    candidate_report: Mapping[str, Any],
) -> dict[str, Any]:
    base_kind, base_id = _source(base_report)
    candidate_kind, candidate_id = _source(candidate_report)
    identity = {
        "base_source_kind": base_kind,
        "candidate_source_kind": candidate_kind,
        "base_source_id": base_id,
        "candidate_source_id": candidate_id,
    }
    if not base_kind or not base_id or not candidate_kind or not candidate_id:
        return {
            "status": "not_comparable",
            "reason": "source_identity_unavailable",
            "identity": identity,
        }
    if (base_kind, base_id) != (candidate_kind, candidate_id):
        return {
            "status": "not_comparable",
            "reason": "source_identity_mismatch",
            "identity": identity,
        }
    base_revision = base_report.get("base_revision_id")
    candidate_revision = candidate_report.get("base_revision_id")
    if not base_revision or not candidate_revision:
        return {
            "status": "not_comparable",
            "reason": "base_revision_unavailable",
            "identity": {
                "source_kind": base_kind,
                "source_id": base_id,
                "base_revision_match": False,
            },
        }
    if base_revision != candidate_revision:
        return {
            "status": "not_comparable",
            "reason": "base_revision_mismatch",
            "identity": {
                "source_kind": base_kind,
                "source_id": base_id,
                "base_revision_match": False,
            },
        }
    return {
        "status": "comparable",
        "identity": {
            "source_kind": base_kind,
            "source_id": base_id,
            "base_revision_match": True,
        },
        "quality": _quality(base_report, candidate_report),
        "complexity": _complexity(base_report, candidate_report),
        "resource": _resource(base_report, candidate_report),
    }
