from __future__ import annotations

from app.evaluation.cad_test_evidence import (
    merge_cad_test_result,
    normalize_cad_test_result,
)


def _result(**overrides):
    value = {
        "status": "pass",
        "case_id": "ctb-00000007",
        "workflow_id": "workflow-1",
        "revision_id": "revision-1",
        "score": {
            "passed": True,
            "geometry_valid": True,
            "requirements_passed": 8,
            "requirements_total": 8,
            "checks": [
                {"id": 1, "status": "pass", "role": "required"},
                {"id": 2, "status": "pass", "role": "diagnostic"},
            ],
            "artifact_sha256": "a" * 64,
        },
        "delivery_verified": True,
        "native_delivery": {
            "passed": True,
            "symmetric_difference_volume_mm3": 0.0,
        },
        "dataset_hash": "dataset-1",
        "scorer_hash": "scorer-1",
        "scorer_image": "image-1",
    }
    value.update(overrides)
    return value


def test_normalize_cad_test_result_preserves_independent_measurements():
    normalized = normalize_cad_test_result(
        _result(),
        expected_workflow_id="workflow-1",
        expected_candidate_revision_id="revision-1",
    )

    assert normalized["schema_version"] == "cad-test-evidence.v1"
    assert normalized["status"] == "passed"
    assert normalized["authority"] == "independent_cad_scorer"
    assert normalized["case"]["id"] == "ctb-00000007"
    assert normalized["requirements"] == {"passed": 8, "total": 8}
    assert normalized["artifact"]["sha256"] == "a" * 64
    assert normalized["delivery"]["status"] == "passed"
    assert normalized["identity"]["workflow_match"] is True


def test_normalize_cad_test_result_never_passes_without_authoritative_evidence():
    normalized = normalize_cad_test_result({"status": "pass", "case_id": "ctb-1"})

    assert normalized["status"] == "unknown"
    assert normalized["verdict"] is None
    assert normalized["reason"] == "cad_test_evidence_incomplete"


def test_normalize_cad_test_result_blocks_identity_mismatch():
    normalized = normalize_cad_test_result(
        _result(workflow_id="other-workflow"),
        expected_workflow_id="workflow-1",
    )

    assert normalized["status"] == "blocked"
    assert normalized["verdict"] is None
    assert normalized["reason"] == "cad_test_identity_mismatch"
    assert normalized["identity"]["workflow_match"] is False


def test_normalize_cad_test_result_blocks_artifact_identity_mismatch():
    normalized = normalize_cad_test_result(
        _result(),
        expected_artifact_sha256="b" * 64,
    )

    assert normalized["status"] == "blocked"
    assert normalized["verdict"] is None
    assert normalized["reason"] == "cad_test_artifact_identity_mismatch"


def test_normalize_cad_test_result_fails_when_required_requirements_are_missing():
    result = _result()
    result["score"]["requirements_passed"] = 0

    normalized = normalize_cad_test_result(result)

    assert normalized["status"] == "failed"
    assert normalized["verdict"] is False
    assert normalized["reason"] == "cad_test_verification_failed"



def test_merge_cad_test_result_updates_quality_matrix_without_changing_business_status():
    report = {
        "status": "finished",
        "workflow_id": "workflow-1",
        "candidate_revision_id": "revision-1",
        "quality_matrix": {"status": "partial", "lifecycle": {"status": "committed"}},
    }

    merged = merge_cad_test_result(report, _result())

    assert merged["status"] == "finished"
    assert merged["cad_test"]["status"] == "passed"
    assert merged["quality_matrix"]["cad_test"]["status"] == "passed"
    assert merged["quality_matrix"]["status"] == "partial"
    assert report.get("cad_test") is None
