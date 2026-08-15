"""Schema validation for the eval case set + compare.py logic.

Hermetic — guards against a malformed case set landing (this runs in CI so a typo
in eval_cases.py fails fast, even though the expensive eval itself does not run in CI).
"""
import asyncio

import pytest

from benchmark.compare import compare_reports
from benchmark.deadline_baseline import apply_pipeline_deadline
from benchmark.eval import _generate_with_deadline
from benchmark.eval_cases import EVAL_CASES, EVAL_CASES_BY_ID, case_set_hash

pytestmark = pytest.mark.unit

VALID_DIFFICULTY = {"simple", "moderate", "complex"}
VALID_PATH = {"extrude_cut", "revolve", "sweep", "loft", "2d", "assembly", "multi_step"}


def test_case_count_is_50():
    assert len(EVAL_CASES) == 50


def test_ids_unique_and_indexed():
    ids = [c["id"] for c in EVAL_CASES]
    assert len(ids) == len(set(ids))
    assert set(EVAL_CASES_BY_ID) == set(ids)


def test_every_case_has_valid_schema():
    for c in EVAL_CASES:
        assert c["id"] and isinstance(c["id"], str)
        assert c["description"].strip()
        assert c["difficulty"] in VALID_DIFFICULTY, c["id"]
        assert c["path"] in VALID_PATH, c["id"]
        if "expected_dims" in c:
            assert isinstance(c["expected_dims"], dict)
        if "expected_features" in c:
            assert isinstance(c["expected_features"], dict)
            for v in c["expected_features"].values():
                assert isinstance(v, int)
        if "tol" in c:
            assert 0 < c["tol"] < 1


def test_path_coverage_present():
    # the eval must exercise every non-extrude path at least once
    paths = {c["path"] for c in EVAL_CASES}
    for required in ("2d", "revolve", "sweep", "loft", "assembly"):
        assert required in paths, f"missing path coverage: {required}"


def test_difficulty_spread():
    diffs = {d: 0 for d in VALID_DIFFICULTY}
    for c in EVAL_CASES:
        diffs[c["difficulty"]] += 1
    # sanity: a real spread from simple to complex
    assert diffs["simple"] >= 10
    assert diffs["complex"] >= 5


def test_case_set_hash_stable():
    assert case_set_hash() == case_set_hash()  # deterministic
    assert len(case_set_hash()) == 12


def test_real_pipeline_eval_enforces_the_product_deadline():
    class HangingOrchestrator:
        cancelled = False

        async def generate(self, *_args, **_kwargs):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    orchestrator = HangingOrchestrator()

    with pytest.raises(TimeoutError):
        asyncio.run(_generate_with_deadline(orchestrator, "test", 0.01))

    assert orchestrator.cancelled is True


# --- compare.py ---------------------------------------------------------------

def _report(case_pass: dict, run_id="r", case_set="h1", rag=True):
    """Build a minimal report shaped like eval.py output for compare tests."""
    cases = []
    for cid, p1 in case_pass.items():
        cases.append({
            "id": cid, "difficulty": "simple", "path": "extrude_cut",
            "agg": {"pass@1": {"mean": p1, "std": 0.0}},
        })
    overall = {
        "n_cases": len(cases),
        "pass@1": {"mean": sum(case_pass.values()) / len(case_pass), "std": 0.1},
        "pass@k": 0.0, "pass^k": 0.0,
        "verdict_pass_rate": {"mean": 0.0, "std": 0.0},
        "printable_rate": {"mean": 0.0, "std": 0.0},
        "one_shot_rate": {"mean": 0.0, "std": 0.0},
        "avg_wall_ms": 1000,
    }
    return {
        "meta": {"run_id": run_id, "case_set_hash": case_set, "rag_enabled": rag,
                 "model": "m"},
        "summary": {"overall": overall, "by_difficulty": {}, "by_path": {}},
        "cases": cases,
    }


def _release_report(
    pass1=1.0,
    *,
    pass1_std=0.0,
    path="extrude_cut",
    artifact_gate=None,
    meta=None,
):
    gate = artifact_gate or {
        "step_readable": True,
        "stl_readable": True,
        "geometry_nonempty": True,
        "rendered_views": 4,
        "passed": True,
    }
    run = {
        "executed": True,
        "passed": pass1 > 0,
        "artifact_gate": gate,
    }
    report = _report({"A": pass1}, case_set="stable")
    report["cases"][0]["path"] = path
    report["cases"][0]["runs"] = [run]
    report["summary"]["overall"]["pass@1"] = {
        "mean": pass1,
        "std": pass1_std,
    }
    report["meta"].update({
        "llm_provider": "moonshot",
        "temperature": 0.2,
        "n_repeats": 1,
        "n_cases": 1,
        "concurrency": 4,
        "pipeline_deadline_s": 180.0,
        "sandbox_runtime": "podman",
        "runtime_identity": {
            "image_digest": "sha256:" + "a" * 64,
            "architecture": "arm64",
        },
    })
    report["meta"].update(meta or {})
    return report


def test_compare_detects_fixed_and_broke():
    base = _report({"A": 1.0, "B": 0.0, "C": 1.0})
    cand = _report({"A": 1.0, "B": 1.0, "C": 0.0})  # B fixed, C broke
    diff = compare_reports(base, cand)
    fixed_ids = {x[0] for x in diff["fixed"]}
    broke_ids = {x[0] for x in diff["broke"]}
    assert fixed_ids == {"B"}
    assert broke_ids == {"C"}
    # C went 100% -> 0% → hard regression
    assert any(x[0] == "C" for x in diff["hard_regressions"])


def test_compare_flags_case_set_change():
    base = _report({"A": 1.0}, case_set="h1")
    cand = _report({"A": 1.0}, case_set="h2")
    diff = compare_reports(base, cand)
    assert diff["case_set_changed"] is True


def test_compare_noise_not_counted_as_change():
    # 1/3 -> 1/3 within threshold → neither fixed nor broke
    base = _report({"A": 0.333})
    cand = _report({"A": 0.333})
    diff = compare_reports(base, cand)
    assert not diff["fixed"]
    assert not diff["broke"]


@pytest.mark.parametrize(
    ("meta_override", "field"),
    [
        ({"model": "other-model"}, "model"),
        ({"llm_provider": "other-provider"}, "llm_provider"),
        ({"temperature": 0.7}, "temperature"),
        ({"rag_enabled": False}, "rag_enabled"),
        ({"n_repeats": 3}, "n_repeats"),
        ({"concurrency": 8}, "concurrency"),
        ({"pipeline_deadline_s": 240.0}, "pipeline_deadline_s"),
        ({"case_set_hash": "different"}, "case_set_hash"),
        (
            {"runtime_identity": {
                "image_digest": "sha256:" + "b" * 64,
                "architecture": "arm64",
            }},
            "runtime_identity.image_digest",
        ),
        (
            {"runtime_identity": {
                "image_digest": "sha256:" + "a" * 64,
                "architecture": "amd64",
            }},
            "runtime_identity.architecture",
        ),
    ],
)
def test_release_gate_rejects_noncomparable_metadata(meta_override, field):
    base = _release_report()
    cand = _release_report(meta=meta_override)

    diff = compare_reports(base, cand)

    assert diff["gate_passed"] is False
    assert field in {mismatch["field"] for mismatch in diff["metadata_mismatches"]}


def test_release_gate_accepts_comparable_report_with_complete_artifacts():
    base = _release_report()
    cand = _release_report()

    diff = compare_reports(base, cand)

    assert diff["gate_failures"] == []
    assert diff["gate_passed"] is True


def test_release_gate_rejects_pass1_below_baseline_standard_deviation():
    base = _release_report(0.8, pass1_std=0.1)
    cand = _release_report(0.69)

    diff = compare_reports(base, cand)

    assert diff["pass1_floor"] == pytest.approx(0.7)
    assert diff["pass1_floor_passed"] is False
    assert diff["gate_passed"] is False


@pytest.mark.parametrize(
    ("gate_override", "reason"),
    [
        ({"step_readable": False, "stl_readable": True, "geometry_nonempty": True, "rendered_views": 4, "passed": False}, "STEP"),
        ({"step_readable": True, "stl_readable": False, "geometry_nonempty": True, "rendered_views": 4, "passed": False}, "STL"),
        ({"step_readable": True, "stl_readable": True, "geometry_nonempty": False, "rendered_views": 4, "passed": False}, "非空几何"),
        ({"step_readable": True, "stl_readable": True, "geometry_nonempty": True, "rendered_views": 3, "passed": False}, "四视图"),
    ],
)
def test_release_gate_rejects_incomplete_success_artifacts(gate_override, reason):
    base = _release_report()
    cand = _release_report(artifact_gate=gate_override)

    diff = compare_reports(base, cand)

    assert diff["gate_passed"] is False
    assert reason in {failure["reason"] for failure in diff["artifact_failures"]}


def test_release_gate_rejects_unreadable_2d_dxf():
    base = _release_report(
        path="2d",
        artifact_gate={"dxf_readable": True, "passed": True},
    )
    cand = _release_report(
        path="2d",
        artifact_gate={"dxf_readable": False, "passed": False},
    )

    diff = compare_reports(base, cand)

    assert diff["artifact_failures"] == [
        {"case_id": "A", "run": 1, "reason": "DXF"},
    ]
    assert diff["gate_passed"] is False


def test_release_gate_rejects_false_success_even_when_report_claims_pass():
    base = _release_report()
    gate = {
        "step_readable": False,
        "stl_readable": True,
        "geometry_nonempty": True,
        "rendered_views": 4,
        "passed": False,
    }
    cand = _release_report(artifact_gate=gate)
    cand["cases"][0]["runs"][0]["passed"] = True

    diff = compare_reports(base, cand)

    assert diff["false_successes"]
    assert diff["gate_passed"] is False


def test_release_gate_rejects_claimed_success_without_execution():
    base = _release_report()
    cand = _release_report()
    cand["cases"][0]["runs"][0]["executed"] = False

    diff = compare_reports(base, cand)

    assert diff["false_successes"]
    assert diff["gate_passed"] is False


def test_deadline_baseline_rejects_only_post_deadline_results_and_reaggregates():
    def run(passed, wall_ms):
        return {
            "executed": passed,
            "verdict": "pass" if passed else None,
            "verdict_pass": passed,
            "watertight": passed,
            "printable": passed,
            "dim_match": passed,
            "part_type_match": None,
            "feature_match": None,
            "dfm_score": None,
            "attempts": 1 if passed else 0,
            "one_shot": passed,
            "exec_time_ms": 100,
            "wall_time_ms": wall_ms,
            "error": None,
            "passed": passed,
            "artifact_gate": {
                "step_readable": passed,
                "stl_readable": passed,
                "geometry_nonempty": passed,
                "rendered_views": 4 if passed else 0,
                "passed": passed,
            },
            "rendered_views": 4 if passed else 0,
            "request_id": "request-id" if passed else None,
        }

    report = {
        "meta": {"run_id": "raw", "total_wall_s": 999},
        "summary": {},
        "cases": [{
            "id": "P01",
            "difficulty": "simple",
            "path": "extrude_cut",
            "description": "test",
            "runs": [run(True, 100_000), run(True, 200_000)],
            "agg": {},
        }],
    }

    normalized = apply_pipeline_deadline(report, 180.0)

    assert report["cases"][0]["runs"][1]["passed"] is True
    assert normalized["meta"]["pipeline_deadline_s"] == 180.0
    assert normalized["meta"]["derived_from_run_id"] == "raw"
    assert normalized["cases"][0]["runs"][0]["passed"] is True
    timed_out = normalized["cases"][0]["runs"][1]
    assert timed_out["executed"] is False
    assert timed_out["passed"] is False
    assert timed_out["error"] == "pipeline deadline exceeded after 180s"
    assert timed_out["observed_post_deadline_wall_ms"] == 200_000
    assert normalized["cases"][0]["agg"]["pass@1"]["mean"] == 0.5
    assert normalized["summary"]["overall"]["pass@1"]["mean"] == 0.5
