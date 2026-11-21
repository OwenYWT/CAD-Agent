import json

import pytest

from benchmark.cad_tests.contracts import (
    CaseVerdict,
    EvaluationCase,
    EvaluationMode,
    EvaluationRun,
    Evidence,
    TestResult as CadTestResult,
    TestSeverity as CadTestSeverity,
    TestStatus as CadTestStatus,
)
from benchmark.cad_tests.report import build_report, write_report
from benchmark.cad_eval import main


pytestmark = pytest.mark.unit


def _case(case_id="P01", status=CadTestStatus.PASS):
    evidence = Evidence(
        evidence_id=f"evidence-{case_id}",
        source_kind="geometry_bbox",
        source_ref="request-1",
        authority="independent_geometry",
        observed={"bbox_extents_mm": [10.0, 10.0, 10.0]},
    )
    test = CadTestResult(
        test_id="geometry.bounding_box.v1",
        status=status,
        severity=CadTestSeverity.REQUIRED,
        message="dimension result",
        evidence_ids=(evidence.evidence_id,),
        failure_code=None if status is CadTestStatus.PASS else "dimension_mismatch",
    )
    return EvaluationCase(
        case_id=case_id,
        input_text="生成一个边长 10 mm 的正方体",
        difficulty="simple",
        modeling_path="extrude_cut",
        tests=(test,),
        result=(
            CaseVerdict.VERIFIED
            if status is CadTestStatus.PASS
            else CaseVerdict.NOT_VERIFIED
            if status is CadTestStatus.UNKNOWN
            else CaseVerdict.FAILED
        ),
        artifacts=(evidence,),
    )


def test_build_report_exposes_versioned_summary_and_evidence_links():
    case = _case()
    run = EvaluationRun(
        run_id="run-1",
        mode=EvaluationMode.OBSERVE,
        provenance={
            "git_sha": "abc",
            "case_set_hash": "def",
            "test_suite_version": "cad-tests.v1",
        },
        cases=(case,),
    )

    report = build_report(run, (case,))
    payload = report.to_dict()

    assert payload["schema_version"] == "cad-evaluation-report.v1"
    assert payload["summary"]["required_total"] == 1
    assert payload["summary"]["required_passed"] == 1
    assert payload["summary"]["required_pass_rate"] == 1.0
    assert payload["summary"]["obligation_coverage"] is None
    assert payload["cases"][0]["tests"][0]["evidence_ids"] == ["evidence-P01"]
    assert payload["cases"][0]["artifacts"][0]["evidence_id"] == "evidence-P01"


def test_build_report_counts_unknown_and_does_not_call_it_pass():
    case = _case(status=CadTestStatus.UNKNOWN)
    run = EvaluationRun(run_id="run-1", mode=EvaluationMode.OBSERVE, cases=(case,))

    payload = build_report(run, (case,)).to_dict()

    assert payload["summary"]["required_total"] == 1
    assert payload["summary"]["required_passed"] == 0
    assert payload["summary"]["required_pass_rate"] == 0.0
    assert payload["summary"]["unknown_count"] == 1
    assert payload["verdict"] == "not_verified"


def test_write_report_is_utf8_and_does_not_include_environment_values(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_SECRET", "do-not-write")
    case = _case()
    run = EvaluationRun(
        run_id="run-1",
        mode=EvaluationMode.OBSERVE,
        provenance={"prompt": case.input_text},
        cases=(case,),
    )
    path = tmp_path / "report.json"

    write_report(build_report(run, (case,)), path)

    content = path.read_text(encoding="utf-8")
    assert "生成一个边长" in content
    assert "do-not-write" not in content
    assert "\ufffd" not in content
    assert json.loads(content)["schema_version"] == "cad-evaluation-report.v1"


def test_disabled_cli_does_not_read_or_write_files(tmp_path):
    missing_input = tmp_path / "missing.json"
    output = tmp_path / "disabled.json"

    exit_code = main(["--mode", "disabled", "--input", str(missing_input), "--output", str(output)])

    assert exit_code == 0
    assert not output.exists()


def test_observe_cli_reads_existing_report_and_writes_developer_report(tmp_path):
    input_path = tmp_path / "benchmark.json"
    output_path = tmp_path / "cad-evaluation.json"
    input_path.write_text(
        json.dumps(
            {
                "meta": {"case_set_hash": "case-hash", "git_sha": "git-sha"},
                "cases": [
                    {
                        "id": "P01",
                        "description": "生成一个边长 10 mm 的正方体",
                        "difficulty": "simple",
                        "path": "extrude_cut",
                        "expected_dims": {"width": 10, "height": 10, "depth": 10},
                        "runs": [
                            {
                                "passed": True,
                                "request_id": "request-1",
                                "evidence": {
                                    "bbox_extents_mm": [10.0, 10.0, 10.0],
                                    "watertight": True,
                                    "printable": True,
                                    "valid_solid": True,
                                    "solid_count": None,
                                },
                                "artifact_gate": {
                                    "step_readable": True,
                                    "stl_readable": True,
                                    "geometry_nonempty": True,
                                    "rendered_views": 4,
                                    "step_sha256": "a" * 64,
                                    "stl_sha256": "b" * 64,
                                },
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--mode",
            "observe",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["run"]["mode"] == "observe"
    assert payload["run"]["provenance"]["case_set_hash"] == "case-hash"
    assert payload["cases"][0]["result"] == "verified"
