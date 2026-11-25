"""Normalize independent CADTest results for live developer reports."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .contracts import validate_payload


_PASS_STATUSES = {"pass", "passed", "verified"}
_ERROR_STATUSES = {"evaluation_error", "error", "scorer_error"}


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = int(value)
    return value if value >= 0 else None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _check_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        row = _mapping(item)
        if row is None:
            continue
        normalized: dict[str, Any] = {}
        for key in ("id", "test_id", "requirement_id", "status", "role", "message", "failure_code"):
            text = _text(row.get(key))
            if text is not None:
                normalized[key] = text
        for key in ("observed", "expected"):
            value = row.get(key)
            if isinstance(value, (str, int, float, bool, list, dict)) or value is None:
                normalized[key] = deepcopy(value)
        if normalized:
            rows.append(normalized)
    return rows


def normalize_cad_test_result(
    result: Mapping[str, Any] | None,
    *,
    expected_workflow_id: str | None = None,
    expected_candidate_revision_id: str | None = None,
    expected_artifact_sha256: str | None = None,
    expected_case_id: str | None = None,
    expected_dataset_hash: str | None = None,
) -> dict[str, Any]:
    """Return a bounded, evidence-based CADTest result.

    A reported ``pass`` is never trusted by itself. The independent scorer must
    provide a boolean score, requirement counts, and a verified artifact
    delivery result before the normalized verdict can be ``passed``.
    """

    if not isinstance(result, Mapping):
        return {
            "schema_version": "cad-test-evidence.v1",
            "status": "unavailable",
            "verdict": None,
            "authority": "unavailable",
            "reason": "cad_test_result_unavailable",
        }

    score = _mapping(result.get("score")) or {}
    native = _mapping(result.get("native_delivery")) or {}
    delivery_verified = _bool(result.get("delivery_verified"))
    native_delivery_passed = _bool(native.get("passed"))
    delivery_evidence_conflict = (
        "delivery_verified" in result
        and "passed" in native
        and (
            delivery_verified is None
            or native_delivery_passed is None
            or delivery_verified != native_delivery_passed
        )
    )
    delivery_passed = delivery_verified
    if delivery_passed is None:
        delivery_passed = native_delivery_passed
    raw_status = str(result.get("status") or "").lower()
    explicit_reason = _text(result.get("reason"))
    workflow_id = _text(result.get("workflow_id"))
    candidate_revision_id = _text(
        result.get("candidate_revision_id") or result.get("revision_id")
    )
    artifact_sha256 = _text(result.get("artifact_sha256") or score.get("artifact_sha256"))
    scorer_artifact_sha256 = _text(
        result.get("scorer_artifact_sha256") or score.get("artifact_sha256")
    )
    case_id = _text(result.get("case_id"))
    dataset_hash = _text(result.get("dataset_hash"))
    workflow_match = (
        None
        if expected_workflow_id is None
        else workflow_id is not None and workflow_id == str(expected_workflow_id)
    )
    revision_match = (
        None
        if expected_candidate_revision_id is None
        else candidate_revision_id is not None
        and candidate_revision_id == str(expected_candidate_revision_id)
    )
    artifact_match = (
        None
        if expected_artifact_sha256 is None
        else artifact_sha256 is not None
        and artifact_sha256 == str(expected_artifact_sha256)
    )
    if expected_artifact_sha256 is None and scorer_artifact_sha256 is not None:
        artifact_match = (
            artifact_sha256 is not None
            and scorer_artifact_sha256 == artifact_sha256
        )
    scorer_artifact_mismatch = (
        scorer_artifact_sha256 is not None
        and (
            artifact_sha256 is None
            or scorer_artifact_sha256 != artifact_sha256
        )
    )
    identity = {
        "workflow_id": workflow_id,
        "candidate_revision_id": candidate_revision_id,
        "workflow_match": workflow_match,
        "candidate_revision_match": revision_match,
        "artifact_sha256": artifact_sha256,
        "scorer_artifact_sha256": scorer_artifact_sha256,
        "artifact_match": artifact_match,
    }
    case_match = None if expected_case_id is None else case_id == str(expected_case_id)
    dataset_match = (
        None
        if expected_dataset_hash is None
        else dataset_hash == str(expected_dataset_hash)
    )
    identity["case_match"] = case_match
    identity["dataset_match"] = dataset_match

    if case_match is False:
        status = "blocked"
        verdict = None
        reason = "cad_test_case_identity_mismatch"
    elif dataset_match is False:
        status = "blocked"
        verdict = None
        reason = "cad_test_dataset_identity_mismatch"
    elif workflow_match is False or revision_match is False:
        status = "blocked"
        verdict = None
        reason = "cad_test_identity_mismatch"
    elif scorer_artifact_mismatch:
        status = "error"
        verdict = None
        reason = "cad_test_scorer_error"
    elif artifact_match is False:
        status = "blocked"
        verdict = None
        reason = "cad_test_artifact_identity_mismatch"
    elif str(result.get("status") or "").lower() == "blocked":
        status = "blocked"
        verdict = None
        reason = _text(result.get("reason")) or "cad_test_identity_mismatch"
    elif str(result.get("status") or "").lower() == "not_applicable":
        status = "not_applicable"
        verdict = None
        reason = _text(result.get("reason")) or "cad_test_not_applicable"
    else:
        scorer_error = (
            _bool(score.get("scorer_error")) is True
            or raw_status in _ERROR_STATUSES
        )
        score_passed = _bool(score.get("passed"))
        geometry_valid = _bool(score.get("geometry_valid"))
        requirements_passed = _non_negative_int(score.get("requirements_passed"))
        requirements_total = _non_negative_int(score.get("requirements_total"))

        if delivery_evidence_conflict:
            status, verdict, reason = "error", None, "cad_test_delivery_evidence_conflict"
        elif scorer_error:
            status, verdict, reason = (
                "error",
                None,
                explicit_reason
                if raw_status == "error" and explicit_reason
                else "cad_test_scorer_error",
            )
        elif (
            score_passed is None
            or geometry_valid is None
            or delivery_passed is None
            or requirements_passed is None
            or requirements_total is None
        ):
            status, verdict, reason = "unknown", None, "cad_test_evidence_incomplete"
        else:
            requirements_passed_all = requirements_passed == requirements_total
            verdict = bool(
                score_passed
                and geometry_valid
                and delivery_passed
                and requirements_passed_all
            )
            status = "passed" if verdict else "failed"
            reason = "cad_test_verified" if verdict else "cad_test_verification_failed"

    authority = (
        "not_applicable"
        if status == "not_applicable"
        else "independent_cad_scorer"
        if status not in {"unavailable", "unknown"}
        else "unavailable"
    )
    normalized: dict[str, Any] = {
        "schema_version": "cad-test-evidence.v1",
        "status": status,
        "verdict": verdict,
        "authority": authority,
        "reason": reason,
        "error_type": _text(result.get("error_type")),
        "case": {
            "id": case_id,
            "dataset_hash": dataset_hash,
            "scorer_hash": _text(result.get("scorer_hash")),
            "scorer_image": _text(result.get("scorer_image")),
        },
        "identity": identity,
        "requirements": {
            "passed": _non_negative_int(score.get("requirements_passed")),
            "total": _non_negative_int(score.get("requirements_total")),
        },
        "checks": _check_rows(score.get("checks")),
        "artifact": {
            "sha256": artifact_sha256,
        },
        "delivery": {
            "status": (
                "error"
                if delivery_evidence_conflict
                else "passed"
                if delivery_passed is True
                else "failed"
                if delivery_passed is False
                else "unknown"
            ),
            "symmetric_difference_volume_mm3": _number(
                native.get("symmetric_difference_volume_mm3")
            ),
            "native_solids": _non_negative_int(native.get("native_solids")),
            "step_solids": _non_negative_int(native.get("step_solids")),
            "stl_facets": _non_negative_int(native.get("stl_facets")),
            "stl_bounds_match": _bool(native.get("stl_bounds_match")),
        },
    }
    validate_payload(normalized)
    return normalized


def merge_cad_test_result(
    report: Mapping[str, Any],
    result: Mapping[str, Any] | None,
    *,
    expected_artifact_sha256: str | None = None,
    expected_case_id: str | None = None,
    expected_dataset_hash: str | None = None,
) -> dict[str, Any]:
    """Attach normalized CADTest evidence without changing business status."""

    expected_workflow_id = _text(report.get("workflow_id"))
    expected_revision_id = _text(report.get("candidate_revision_id"))
    normalized = normalize_cad_test_result(
        result,
        expected_workflow_id=expected_workflow_id,
        expected_candidate_revision_id=expected_revision_id,
        expected_artifact_sha256=expected_artifact_sha256,
        expected_case_id=expected_case_id,
        expected_dataset_hash=expected_dataset_hash,
    )
    merged = dict(report)
    merged["cad_test"] = normalized
    matrix = deepcopy(_mapping(report.get("quality_matrix")) or {})
    matrix["cad_test"] = {
        "status": normalized["status"],
        "authority": normalized["authority"],
        "reason": normalized["reason"],
    }
    merged["quality_matrix"] = matrix
    return merged
