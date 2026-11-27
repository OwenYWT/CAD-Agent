"""Unit tests for the eval harness metric extraction (benchmark/metrics.py).

Hermetic — no LLM / Docker. Builds real GenerateResponse / InspectReport / etc.
pydantic objects so the tests pin the actual schema field names the harness reads.
"""
import pytest

from app.models.schemas import (
    BoundingBox,
    CADPlan,
    GenerateResponse,
    InspectCheck,
    InspectReport,
    ValidationResult,
)
from benchmark import metrics as M

pytestmark = pytest.mark.unit


def _bbox(dx, dy, dz):
    return BoundingBox(x_min=0, x_max=dx, y_min=0, y_max=dy, z_min=0, z_max=dz)


def _plan(part_type="custom", features=None):
    return CADPlan(
        description="t", part_type=part_type, dimensions={},
        features=features or [], constraints=[], ambiguities=[], modeling_hint="",
    )


def _resp(**kw):
    base = dict(request_id="r", success=True, attempts=1)
    base.update(kw)
    return GenerateResponse(**base)


# --- parse_feature_counts -----------------------------------------------------

def test_parse_feature_counts_through_hole():
    feats = ["through_hole:diameter=3.2,count=4,pattern=rectangular", "fillet:radius=2"]
    assert M.parse_feature_counts(feats) == {"holes": 4}


def test_parse_feature_counts_multiple_hole_features():
    feats = ["hole:count=2", "cbore:count=1", "shell:thickness=2"]
    assert M.parse_feature_counts(feats)["holes"] == 3


def test_parse_feature_counts_hole_without_count_defaults_one():
    assert M.parse_feature_counts(["counterbore:diameter=5"])["holes"] == 1


def test_parse_feature_counts_defensive_on_garbage():
    # non-string / malformed entries must not raise
    assert M.parse_feature_counts([123, "noformat", "shell:thickness=2"])["holes"] == 0
    assert M.parse_feature_counts(None) == {"holes": 0}


# --- dims_match ---------------------------------------------------------------

def test_dims_match_orientation_independent():
    # expected 50x30x20 vs actual produced rotated (20x50x30) → still matches
    assert M.dims_match(_bbox(20, 50, 30), {"width": 50, "height": 30, "depth": 20}) is True


def test_dims_match_out_of_tolerance():
    assert M.dims_match(_bbox(50, 30, 40), {"width": 50, "height": 30, "depth": 20}) is False


def test_dims_match_none_when_no_expected():
    assert M.dims_match(_bbox(1, 2, 3), {}) is None
    assert M.dims_match(None, {"width": 10}) is None


# --- extract_metrics: 3D printable happy path ---------------------------------

def test_extract_3d_printable_pass():
    insp = InspectReport(verdict="pass", printable=True, is_watertight=True,
                         bounding_box=_bbox(50, 30, 20), volume=30000.0)
    val = ValidationResult(is_watertight=True, bounding_box=_bbox(50, 30, 20),
                           volume=30000.0, printable=True, fits_build_volume=True)
    resp = _resp(validation=val, inspect_report=insp, plan=_plan())
    case = {"path": "extrude_cut", "expected_dims": {"width": 50, "height": 30, "depth": 20}}
    m = M.extract_metrics(resp, case, wall_time_ms=1234)
    assert m["passed"] is True
    assert m["verdict_pass"] is True
    assert m["printable"] is True
    assert m["dim_match"] is True
    assert m["one_shot"] is True
    assert m["wall_time_ms"] == 1234


def test_extract_3d_not_printable_fails_even_if_success():
    # the whole point: success=True but not printable → NOT passed
    insp = InspectReport(verdict="fail", printable=False, is_watertight=False,
                         bounding_box=_bbox(50, 30, 20))
    val = ValidationResult(is_watertight=False, printable=False, fits_build_volume=True,
                           bounding_box=_bbox(50, 30, 20))
    resp = _resp(success=True, attempts=5, validation=val, inspect_report=insp, plan=_plan())
    m = M.extract_metrics(resp, {"path": "extrude_cut"})
    assert m["passed"] is False
    assert m["verdict_pass"] is False
    assert m["one_shot"] is False


def test_extract_dim_mismatch_blocks_pass():
    insp = InspectReport(verdict="pass", printable=True, is_watertight=True,
                         bounding_box=_bbox(99, 99, 99))
    val = ValidationResult(is_watertight=True, printable=True, fits_build_volume=True,
                           bounding_box=_bbox(99, 99, 99))
    resp = _resp(validation=val, inspect_report=insp, plan=_plan())
    case = {"path": "extrude_cut", "expected_dims": {"width": 50, "height": 30, "depth": 20}}
    m = M.extract_metrics(resp, case)
    assert m["dim_match"] is False
    assert m["passed"] is False  # geometry-fact gate


def test_part_type_and_feature_are_diagnostic_not_gating():
    # planner misclassified part_type and got hole count wrong, but geometry is fine
    insp = InspectReport(verdict="pass", printable=True, is_watertight=True,
                         bounding_box=_bbox(50, 30, 20))
    val = ValidationResult(is_watertight=True, printable=True, fits_build_volume=True,
                           bounding_box=_bbox(50, 30, 20))
    resp = _resp(validation=val, inspect_report=insp,
                 plan=_plan(part_type="box", features=["hole:count=1"]))
    case = {"path": "extrude_cut", "expected_part_type": "custom",
            "expected_features": {"holes": 4}}
    m = M.extract_metrics(resp, case)
    assert m["part_type_match"] is False
    assert m["feature_match"] is False
    assert m["passed"] is True  # still passes — diagnostics don't gate


# --- extract_metrics: 2D path -------------------------------------------------

def test_extract_2d_pass_on_dxf_output():
    resp = _resp(files={"dxf": "/api/files/r/result.dxf", "svg": "/api/files/r/result.svg"},
                 plan=_plan(part_type="profile_2d"))
    m = M.extract_metrics(resp, {"path": "2d", "expected_part_type": "profile_2d"})
    assert m["passed"] is True
    # 2D has no printability/watertight signal
    assert m["printable"] is None


def test_extract_2d_fails_without_2d_file():
    resp = _resp(files={"step": "x", "stl": "y"}, plan=_plan())
    m = M.extract_metrics(resp, {"path": "2d"})
    assert m["passed"] is False


# --- extract_metrics: None / crash --------------------------------------------

def test_extract_none_response_is_failure():
    m = M.extract_metrics(None, {"path": "extrude_cut"}, wall_time_ms=50)
    assert m["passed"] is False
    assert m["executed"] is False
    assert m["error"]
    assert m["wall_time_ms"] == 50


def test_extract_success_without_inspect_report():
    # success but inspect_report is None (no STL / validator raised) → not passed,
    # must not crash
    resp = _resp(success=True, attempts=2, validation=None, inspect_report=None, plan=_plan())
    m = M.extract_metrics(resp, {"path": "extrude_cut"})
    assert m["executed"] is True
    assert m["printable"] is None
    assert m["passed"] is False


def test_extract_assembly_should_not_be_printable():
    # assembly case: should_be_printable False → requires inspect present + verdict,
    # not printable==True
    insp = InspectReport(verdict="warn", printable=None, is_watertight=False,
                         bounding_box=_bbox(80, 80, 110))
    val = ValidationResult(bounding_box=_bbox(80, 80, 110))
    resp = _resp(validation=val, inspect_report=insp, plan=_plan(part_type="assembly"))
    case = {"path": "assembly", "should_be_printable": False}
    m = M.extract_metrics(resp, case)
    assert m["passed"] is True


# --- immutable artifact gate -------------------------------------------------

def test_artifact_gate_requires_all_real_3d_evidence():
    row = {"passed": True}
    evidence = {
        "step_readable": True,
        "stl_readable": True,
        "geometry_nonempty": True,
        "rendered_views": 4,
    }

    gated = M.apply_artifact_gate(row, {"path": "extrude_cut"}, evidence)

    assert gated["passed"] is True
    assert gated["artifact_gate"]["passed"] is True


@pytest.mark.parametrize(
    ("missing_key", "value"),
    [
        ("step_readable", False),
        ("stl_readable", False),
        ("geometry_nonempty", False),
        ("rendered_views", 3),
    ],
)
def test_artifact_gate_rejects_false_success_for_3d(missing_key, value):
    evidence = {
        "step_readable": True,
        "stl_readable": True,
        "geometry_nonempty": True,
        "rendered_views": 4,
    }
    evidence[missing_key] = value

    gated = M.apply_artifact_gate({"passed": True}, {"path": "extrude_cut"}, evidence)

    assert gated["passed"] is False
    assert gated["artifact_gate"]["passed"] is False


def test_artifact_gate_requires_parseable_dxf_for_2d():
    accepted = M.apply_artifact_gate(
        {"passed": True},
        {"path": "2d"},
        {"dxf_readable": True},
    )
    rejected = M.apply_artifact_gate(
        {"passed": True},
        {"path": "2d"},
        {"dxf_readable": False},
    )

    assert accepted["passed"] is True
    assert rejected["passed"] is False


def test_artifact_gate_never_turns_failed_geometry_into_success():
    gated = M.apply_artifact_gate(
        {"passed": False},
        {"path": "extrude_cut"},
        {
            "step_readable": True,
            "stl_readable": True,
            "geometry_nonempty": True,
            "rendered_views": 4,
        },
    )

    assert gated["passed"] is False


# --- aggregation --------------------------------------------------------------

def _row(passed, verdict_pass=None, printable=None, one_shot=False, attempts=1, wall=100):
    return {
        "passed": passed,
        "verdict_pass": verdict_pass if verdict_pass is not None else passed,
        "printable": printable if printable is not None else passed,
        "watertight": passed,
        "one_shot": one_shot,
        "dim_match": passed,
        "attempts": attempts,
        "wall_time_ms": wall,
    }


def test_aggregate_case_pass_at_k():
    runs = [_row(True), _row(False), _row(False)]
    agg = M.aggregate_case(runs)
    assert agg["pass@1"]["mean"] == pytest.approx(1 / 3, abs=1e-3)
    assert agg["pass@k"] is True      # at least one passed
    assert agg["pass^k"] is False     # not all passed


def test_aggregate_case_all_pass():
    runs = [_row(True, one_shot=True), _row(True), _row(True)]
    agg = M.aggregate_case(runs)
    assert agg["pass@1"]["mean"] == 1.0
    assert agg["pass@k"] is True
    assert agg["pass^k"] is True


def test_aggregate_case_accepts_a_failed_batch_run_without_response_attempts():
    run = {
        "executed": False,
        "passed": False,
        "error": "RuntimeError: workflow submission failed",
        "process_evaluation": {"status": "unavailable"},
        "wall_time_ms": 12,
    }

    aggregate = M.aggregate_case([run])

    assert aggregate["avg_attempts"] == 0
    assert aggregate["final_gate_status"]["blocked"] == 1


def test_aggregate_summary_groups():
    cases = [
        {"id": "A", "difficulty": "basic", "path": "extrude_cut",
         "agg": M.aggregate_case([_row(True), _row(True), _row(True)])},
        {"id": "B", "difficulty": "combined", "path": "revolve",
         "agg": M.aggregate_case([_row(False), _row(False), _row(False)])},
        {"id": "C", "difficulty": "complex", "path": "extrude_cut",
         "agg": M.aggregate_case([_row(False), _row(False), _row(False)])},
    ]
    s = M.aggregate_summary(cases)
    assert s["overall"]["n_cases"] == 3
    assert s["overall"]["pass@1"]["mean"] == pytest.approx(1 / 3, abs=0.0001)
    assert list(s["by_difficulty"]) == ["basic", "combined", "complex"]
    assert s["by_difficulty"]["basic"]["pass@1"]["mean"] == 1.0
    assert s["by_difficulty"]["combined"]["pass@1"]["mean"] == 0.0
    assert s["by_difficulty"]["complex"]["pass@1"]["mean"] == 0.0
    assert s["by_path"]["revolve"]["pass@1"]["mean"] == 0.0


def test_aggregate_summary_exposes_partial_pass_metric_coverage():
    passed_runs = [_row(True), _row(True)]
    unavailable_runs = [_row(None), _row(None)]
    cases = [
        {"id": case_id, "agg": M.aggregate_case(passed_runs)}
        for case_id in ("A", "B")
    ]
    cases.append({"id": "C", "agg": M.aggregate_case(unavailable_runs)})

    overall = M.aggregate_summary(cases)["overall"]

    assert overall["pass@1"]["mean"] == 1.0
    assert overall["pass@k"] == 1.0
    assert overall["pass^k"] == 1.0
    assert overall["pass_metric_coverage"] == {
        "scored_cases": 2,
        "total_cases": 3,
        "case_coverage": pytest.approx(2 / 3),
        "scored_runs": 4,
        "total_runs": 6,
        "run_coverage": pytest.approx(2 / 3),
    }


def test_aggregate_process_evaluation_metrics_are_reported():
    runs = [
        {
            **_row(True),
            "process_evaluation": {
                "status": "measured",
                "workflow_activity_count": 8,
                "cad_operation_count": 3,
                "repair_count": 1,
                "retry_call_count": 1,
                "failed_call_count": 0,
                "geometry_status": "passed",
                "dfm_status": "passed",
                "visual_status": "passed",
            },
        },
        {
            **_row(False),
            "process_evaluation": {
                "status": "measured",
                "workflow_activity_count": 10,
                "cad_operation_count": 4,
                "repair_count": 2,
                "retry_call_count": 2,
                "failed_call_count": 1,
                "geometry_status": "failed",
                "dfm_status": "unknown",
                "visual_status": "unavailable",
            },
        },
    ]

    aggregate = M.aggregate_case(runs)

    assert aggregate["process_evaluation"] == {
        "measured_rate": 1.0,
        "avg_workflow_activity_count": 9.0,
        "avg_cad_operation_count": 3.5,
        "avg_repair_count": 1.5,
        "avg_retry_call_count": 1.5,
        "avg_failed_call_count": 0.5,
        "avg_total_call_count": 0.0,
        "avg_activity_duration_ms": 0.0,
        "geometry_pass_rate": 0.5,
        "dfm_pass_rate": 0.5,
        "visual_pass_rate": 0.5,
        "duration_measured_rate": 0.0,
        "repair_gain_measured_rate": 0.0,
    }


def test_aggregate_missing_legacy_metrics_are_unavailable_not_failed():
    run = {
        "executed": True,
        "passed": True,
        "verdict": None,
        "verdict_pass": False,
        "printable": None,
        "watertight": None,
        "one_shot": None,
        "dim_match": None,
        "attempts": 0,
        "wall_time_ms": 100,
        "cad_test": {"status": "passed"},
        "artifact_gate": {"passed": True},
        "process_evaluation": {"status": "measured"},
    }

    aggregate = M.aggregate_case([run])

    for metric in (
        "verdict_pass_rate",
        "printable_rate",
        "watertight_rate",
        "one_shot_rate",
        "dim_match_rate",
    ):
        assert aggregate[metric]["mean"] is None
        assert aggregate[metric]["n"] == 0
        assert aggregate[metric]["total"] == 1

    assert aggregate["final_gate_status"] == {
        "passed": 1,
        "failed": 0,
        "blocked": 0,
        "unavailable": 0,
        "pending": 0,
        "not_applicable": 0,
        "total": 1,
    }
    assert aggregate["cad_test_status"]["passed"] == 1
    assert aggregate["cad_test_status"]["unavailable"] == 0
    assert aggregate["process_evaluation_status"]["measured"] == 1


def test_extract_missing_authoritative_fields_are_none():
    response = _resp(inspect_report=None, validation=None, attempts=0)

    metrics = M.extract_metrics(response, {"path": "extrude_cut"})

    assert metrics["verdict_pass"] is None
    assert metrics["printable"] is None
    assert metrics["watertight"] is None
    assert metrics["one_shot"] is None
    assert metrics["dim_match"] is None


def test_aggregate_summary_preserves_unavailable_status_counts():
    case = {
        "id": "unavailable",
        "difficulty": "simple",
        "path": "extrude_cut",
        "runs": [{
            "executed": False,
            "passed": False,
            "verdict_pass": None,
            "printable": None,
            "watertight": None,
            "one_shot": None,
            "dim_match": None,
            "attempts": 0,
            "wall_time_ms": 0,
            "process_evaluation": {"status": "unavailable"},
        }],
    }
    case["agg"] = M.aggregate_case(case["runs"])

    overall = M.aggregate_summary([case])["overall"]

    assert overall["verdict_pass_rate"]["mean"] is None
    assert overall["final_gate_status"] == {
        "passed": 0,
        "failed": 0,
        "blocked": 1,
        "unavailable": 0,
        "pending": 0,
        "not_applicable": 0,
        "total": 1,
    }
    assert overall["process_evaluation_status"]["unavailable"] == 1
