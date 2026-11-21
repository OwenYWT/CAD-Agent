"""Derive measured complexity and quality summaries from live evidence."""

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


def _validation_rows(
    report: Mapping[str, Any], event_type: str
) -> list[tuple[Mapping[str, Any], str, Mapping[str, Any]]]:
    evidence_by_id = _evidence_by_id(report)
    rows = []
    for event in _timeline(report):
        if event.get("event_type") != event_type:
            continue
        evidence_ids = event.get("evidence_ids")
        if not isinstance(evidence_ids, list):
            continue
        for evidence_id in evidence_ids:
            evidence = evidence_by_id.get(str(evidence_id))
            observed = evidence.get("observed") if evidence else None
            if isinstance(observed, Mapping):
                rows.append((event, str(evidence_id), observed))
    return rows


def _number(value: Any) -> int | float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    return value


def _dfm_face_measurement(report: Mapping[str, Any]) -> tuple[int | float | None, str | None]:
    for _, evidence_id, observed in _validation_rows(report, "dfm_validation_finished"):
        metrics = observed.get("metrics")
        if not isinstance(metrics, Mapping):
            continue
        face_count = _number(metrics.get("face_count"))
        if face_count is not None:
            return face_count, evidence_id
    return None, None


def _geometry_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    rows = _validation_rows(report, "geometry_validation_finished")
    artifacts: list[tuple[str, Mapping[str, Any]]] = []
    for _, evidence_id, observed in rows:
        value = observed.get("artifacts")
        if not isinstance(value, list):
            continue
        for artifact in value:
            if isinstance(artifact, Mapping):
                artifacts.append((evidence_id, artifact))
    if not artifacts:
        return {
            "status": "unavailable",
            "reason": "geometry_complexity_evidence_unavailable",
        }

    evidence_ids = sorted({evidence_id for evidence_id, _ in artifacts})
    _, representative = artifacts[0]
    dimensions = representative.get("dimensions_mm")
    dimensions = (
        [float(value) for value in dimensions]
        if isinstance(dimensions, list)
        and len(dimensions) == 3
        and all(_number(value) is not None for value in dimensions)
        else None
    )
    face_count = _number(representative.get("face_count"))
    face_evidence_id = None
    if face_count is None:
        face_count, face_evidence_id = _dfm_face_measurement(report)
    fields = {
        "dimensions_mm": dimensions,
        "solid_count": _number(representative.get("solid_count")),
        "face_count": face_count,
        "volume_mm3": _number(representative.get("volume_mm3")),
    }
    if face_evidence_id is not None:
        evidence_ids.append(face_evidence_id)
    result: dict[str, Any] = {
        "status": "measured" if all(value is not None for value in fields.values()) else "partial",
        **fields,
        "artifact_count": len(artifacts),
        "evidence_ids": sorted(set(evidence_ids)),
    }
    return result


def _operation_summary(
    report: Mapping[str, Any], repair_gain: Mapping[str, Any] | None
) -> dict[str, Any]:
    calls = report.get("tool_calls")
    calls = calls if isinstance(calls, list) else []
    cad_operation_count = sum(
        1 for call in calls if isinstance(call, Mapping) and call.get("call_kind") == "cad_operation"
    )
    workflow_activity_count = sum(
        1
        for call in calls
        if isinstance(call, Mapping) and call.get("call_kind") == "workflow_activity"
    )
    candidate_count = sum(
        1 for event in _timeline(report) if event.get("event_type") == "candidate_sealed"
    )
    repair_count = 0
    if isinstance(repair_gain, Mapping):
        repair_count = int(repair_gain.get("repair_count") or 0)
    return {
        "status": "measured",
        "cad_operation_count": cad_operation_count,
        "workflow_activity_count": workflow_activity_count,
        "repair_count": repair_count,
        "candidate_count": candidate_count,
        "dependency_depth": None,
    }


def _quality_status(
    report: Mapping[str, Any], event_type: str
) -> dict[str, Any]:
    rows = _validation_rows(report, event_type)
    if not rows:
        return {"status": "unavailable", "evidence_ids": []}
    event, evidence_id, _ = rows[-1]
    status = str(event.get("status") or "unknown").lower()
    if status in {"pass", "passed", "success", "succeeded"}:
        status = "passed"
    elif status in {"failed", "fail", "error", "rejected"}:
        status = "failed"
    else:
        status = "unknown"
    return {"status": status, "evidence_ids": [evidence_id]}


def _visual_quality(report: Mapping[str, Any]) -> dict[str, Any]:
    events = [
        event
        for event in _timeline(report)
        if event.get("event_type") == "visual_validation_finished"
    ]
    if not events:
        return {"status": "unavailable", "evidence_ids": []}
    event = events[-1]
    status = str(event.get("status") or "unknown").lower()
    if status in {"passed", "success", "succeeded", "pass"}:
        status = "passed"
    elif status in {"failed", "fail", "error", "rejected"}:
        status = "failed"
    else:
        status = "unknown"
    evidence_ids = event.get("evidence_ids")
    evidence_ids = [str(value) for value in evidence_ids] if isinstance(evidence_ids, list) else []
    return {"status": status, "evidence_ids": evidence_ids}


def _artifact_quality(geometry: Mapping[str, Any]) -> dict[str, Any]:
    count = geometry.get("artifact_count")
    if not isinstance(count, int) or count < 1:
        return {"status": "unavailable", "artifact_count": 0}
    return {"status": "measured", "artifact_count": count}


def _lifecycle_quality(report: Mapping[str, Any]) -> dict[str, Any]:
    events = [
        event for event in _timeline(report) if event.get("event_type") == "session_finished"
    ]
    if not events:
        return {"status": "unavailable"}
    payload = events[-1].get("payload")
    business_status = payload.get("business_status") if isinstance(payload, Mapping) else None
    return {"status": str(business_status or report.get("status") or "unknown")}


def build_complexity_profile(
    report: Mapping[str, Any],
    *,
    repair_gain: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    geometry = _geometry_summary(report)
    operation = _operation_summary(report, repair_gain)
    profile = {
        "status": "partial",
        "text": {
            "status": "unavailable",
            "reason": "prompt_complexity_not_recorded",
        },
        "geometry": geometry,
        "operation": operation,
        "engineering": {
            "status": "unavailable",
            "reason": "engineering_complexity_not_recorded",
        },
        "input_modality": {
            "status": "unavailable",
            "reason": "input_modality_not_recorded",
        },
    }
    return profile


def build_quality_matrix(report: Mapping[str, Any]) -> dict[str, Any]:
    geometry = _quality_status(report, "geometry_validation_finished")
    dfm = _quality_status(report, "dfm_validation_finished")
    visual = _visual_quality(report)
    geometry_complexity = _geometry_summary(report)
    artifact = _artifact_quality(geometry_complexity)
    lifecycle = _lifecycle_quality(report)
    statuses = [geometry["status"], dfm["status"], visual["status"], artifact["status"]]
    overall = "measured" if all(status in {"passed", "measured"} for status in statuses) else "partial"
    return {
        "status": overall,
        "geometry": geometry,
        "dfm": dfm,
        "visual": visual,
        "artifact": artifact,
        "lifecycle": lifecycle,
    }
