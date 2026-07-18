"""Schema validation for the eval case set + compare.py logic.

Hermetic — guards against a malformed case set landing (this runs in CI so a typo
in eval_cases.py fails fast, even though the expensive eval itself does not run in CI).
"""
import pytest

from benchmark.compare import compare_reports
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
