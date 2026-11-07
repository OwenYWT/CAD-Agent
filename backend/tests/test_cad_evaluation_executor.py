import pytest

from benchmark.cad_tests.contracts import (
    Evidence,
    TestDefinition as CadTestDefinition,
    TestSeverity as CadTestSeverity,
)
from benchmark.cad_tests.definitions import v0_definitions
from benchmark.cad_tests.evidence import EvidenceContext
from benchmark.cad_tests.executor import execute_case


pytestmark = pytest.mark.unit


def _context(**overrides):
    observations = {
        "bbox_extents_mm": [10.0, 20.0, 30.0],
        "watertight": True,
        "printable": True,
        "solid_count": 1,
        "valid_solid": True,
        "artifact_gate": {
            "step_readable": True,
            "stl_readable": True,
            "geometry_nonempty": True,
            "rendered_views": 4,
            "step_sha256": "a" * 64,
            "stl_sha256": "b" * 64,
        },
    }
    observations.update(overrides)
    evidence = (
        Evidence(
            evidence_id="geometry-bbox",
            source_kind="geometry_bbox",
            source_ref="request-1",
            authority="independent_geometry",
            observed={"bbox_extents_mm": observations["bbox_extents_mm"]},
        ),
    )
    case = {
        "id": "P01",
        "description": "make a 10 x 20 x 30 mm part",
        "difficulty": "simple",
        "path": "extrude_cut",
        "expected_dims": {"width": 10, "height": 20, "depth": 30},
        "tol": 0.1,
        "should_be_printable": True,
    }
    return case, EvidenceContext(case=case, row={}, observations=observations, evidence=evidence)


def test_all_required_v0_tests_pass_when_authoritative_evidence_is_present():
    case, context = _context()

    evaluated = execute_case(case, context, definitions=v0_definitions(case))

    assert evaluated.result.value == "verified"
    assert all(item.status.value == "pass" for item in evaluated.tests if item.severity.value == "required")


def test_dimension_mismatch_is_a_required_failure():
    case, context = _context(bbox_extents_mm=[10.0, 20.0, 99.0])

    evaluated = execute_case(case, context, definitions=v0_definitions(case))

    dimension = next(item for item in evaluated.tests if item.test_id == "geometry.bounding_box.v1")
    assert dimension.status.value == "fail"
    assert dimension.failure_code == "dimension_mismatch"
    assert evaluated.result.value == "failed"


def test_missing_dimension_evidence_is_not_verified():
    case, context = _context(bbox_extents_mm=None)

    evaluated = execute_case(case, context, definitions=v0_definitions(case))

    dimension = next(item for item in evaluated.tests if item.test_id == "geometry.bounding_box.v1")
    assert dimension.status.value == "unknown"
    assert dimension.failure_code == "missing_dimension_evidence"
    assert evaluated.result.value == "not_verified"


def test_missing_artifact_dependency_is_blocked():
    case, context = _context(artifact_gate={})

    evaluated = execute_case(case, context, definitions=v0_definitions(case))

    artifact = next(item for item in evaluated.tests if item.test_id == "artifact.readable.v1")
    assert artifact.status.value == "blocked"
    assert artifact.failure_code == "missing_artifact"
    assert evaluated.result.value == "blocked"


def test_unknown_test_kind_is_an_error_and_never_a_pass():
    case, context = _context()
    definitions = (
        CadTestDefinition(
            test_id="unsupported.v1",
            test_version="1",
            kind="unsupported",
            severity=CadTestSeverity.REQUIRED,
        ),
    )

    evaluated = execute_case(case, context, definitions=definitions)

    assert evaluated.tests[0].status.value == "error"
    assert evaluated.tests[0].failure_code == "unsupported_test_kind"
    assert evaluated.result.value == "error"


def test_diagnostic_failure_does_not_fail_verified_required_tests():
    case, context = _context()
    definitions = (
        CadTestDefinition(
            test_id="geometry.valid_solid.v1",
            test_version="1",
            kind="geometry.valid_solid",
            severity=CadTestSeverity.REQUIRED,
        ),
        CadTestDefinition(
            test_id="geometry.solid_count.v1",
            test_version="1",
            kind="geometry.solid_count",
            severity=CadTestSeverity.DIAGNOSTIC,
            parameters={"expected": 2},
        ),
    )

    evaluated = execute_case(case, context, definitions=definitions)

    assert evaluated.result.value == "verified"
    assert evaluated.tests[1].status.value == "fail"


def test_solid_count_requires_explicit_observation():
    case, context = _context(solid_count=None)
    case["expected_solid_count"] = 1

    evaluated = execute_case(case, context, definitions=v0_definitions(case))

    solid_count = next(item for item in evaluated.tests if item.test_id == "geometry.solid_count.v1")
    assert solid_count.status.value == "unknown"
    assert solid_count.failure_code == "solid_count_unavailable"


def test_artifact_hash_requires_a_referenced_file(tmp_path):
    case, context = _context()
    definitions = (
        CadTestDefinition(
            test_id="provenance.artifact_hash.v1",
            test_version="1",
            kind="provenance.artifact_hash",
            severity=CadTestSeverity.REQUIRED,
            parameters={"formats": ("step",)},
        ),
    )

    evaluated = execute_case(case, context, definitions=definitions)

    assert evaluated.tests[0].status.value == "unknown"
    assert evaluated.tests[0].failure_code == "artifact_source_unavailable"
