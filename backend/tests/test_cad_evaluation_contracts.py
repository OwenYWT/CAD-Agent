import json

import pytest

from benchmark.cad_tests.contracts import (
    CaseVerdict,
    EvaluationCase,
    EvaluationReport,
    EvaluationRun,
    Evidence,
    TestDefinition as CadTestDefinition,
    TestResult as CadTestResult,
    TestSeverity as CadTestSeverity,
    TestStatus as CadTestStatus,
)
from benchmark.cad_tests.mode import EvaluationMode


pytestmark = pytest.mark.unit


def test_versioned_report_round_trips_json_compatible_contracts():
    evidence = Evidence(
        evidence_id="evidence-1",
        source_kind="geometry_validator",
        source_ref="request-1",
        authority="independent_geometry",
        artifact_sha256="a" * 64,
        observed={"width_mm": 10.0},
    )
    definition = CadTestDefinition(
        test_id="geometry.bounding_box.v1",
        test_version="1",
        kind="geometry.bounding_box",
        severity=CadTestSeverity.REQUIRED,
        parameters={"expected": {"width": 10.0}, "tolerance": 0.1},
    )
    result = CadTestResult(
        test_id=definition.test_id,
        status=CadTestStatus.PASS,
        severity=definition.severity,
        message="dimension matches",
        evidence_ids=(evidence.evidence_id,),
        observed={"width_mm": 10.0},
        expected={"width_mm": 10.0},
    )
    case = EvaluationCase(
        case_id="P01",
        input_text="make a 10 mm cube",
        difficulty="simple",
        modeling_path="extrude_cut",
        tests=(result,),
        result=CaseVerdict.VERIFIED,
        artifacts=(evidence,),
    )
    run = EvaluationRun(
        run_id="run-1",
        mode=EvaluationMode.OBSERVE,
        provenance={"test_suite_version": "cad-tests.v1"},
        cases=(case,),
    )
    report = EvaluationReport(
        run=run,
        summary={"required_pass_rate": 1.0},
        verdict=CaseVerdict.VERIFIED,
        cases=(case,),
    )

    encoded = json.dumps(report.to_dict(), ensure_ascii=False)
    decoded = json.loads(encoded)

    assert decoded["schema_version"] == "cad-evaluation-report.v1"
    assert decoded["run"]["mode"] == "observe"
    assert decoded["cases"][0]["tests"][0]["status"] == "pass"
    assert decoded["cases"][0]["tests"][0]["severity"] == "required"
    assert decoded["cases"][0]["tests"][0]["evidence_ids"] == ["evidence-1"]
    assert decoded["verdict"] == "verified"


@pytest.mark.parametrize("status", list(CadTestStatus))
def test_all_test_statuses_are_serialized_without_coercion(status):
    result = CadTestResult(
        test_id="test.v1",
        status=status,
        severity=CadTestSeverity.REQUIRED,
        message="status fixture",
    )

    assert result.to_dict()["status"] == status.value


def test_invalid_test_definition_is_rejected():
    with pytest.raises(ValueError, match="test_id"):
        CadTestDefinition(
            test_id="",
            test_version="1",
            kind="geometry.valid_solid",
            severity=CadTestSeverity.REQUIRED,
        )
