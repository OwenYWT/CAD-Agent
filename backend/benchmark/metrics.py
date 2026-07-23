"""Layered metric extraction for the CAD Agent eval harness.

Pure functions, no LLM / Docker / network. Everything here is computed from an
already-produced `GenerateResponse` plus the case spec, so this module is cheap to
import and trivial to unit-test (the response is duck-typed — tests can pass a
SimpleNamespace).

Metric layers (see benchmark plan):
  L0 执行    : executed                          (response.success)
  L1 几何    : watertight / printable / verdict  (validation / inspect_report)
  L2 意图    : dim_match / part_type_match / feature_match
  L3 制造    : dfm_score                         (dfm_analysis.design_score)
  过程       : attempts / one_shot / exec_time_ms

The headline per-case judgment is `passed`, which is PATH-AWARE:
  - 2D cases  : executed AND a 2D file (dxf/svg) was produced AND intent checks ok
  - 3D cases  : executed AND printable-as-expected AND intent checks ok

`verdict_pass` (inspect_report.verdict == "pass") is tracked separately as the
honesty-driven geometry headline; `passed` is the practically-usable judgment.

Design note on the `is not False` pattern: an intent check is `None` when it was
not requested for a case (no expected_dims, etc.). `None` must NEVER block a pass —
only an explicit `False` does. So composites use `x is not False`.
"""
from __future__ import annotations

import re

# Feature names (the part before ':' in a plan.features string) that denote a hole.
_HOLE_FEATURE_RE = re.compile(r"hole|bore|cbore|csk|counterbore|countersink", re.I)


def parse_feature_counts(features) -> dict:
    """Parse CADPlan.features (list of 'name:k=v,k=v' strings) into aggregate counts.

    Returns a dict currently carrying {'holes': <int>}. Defensive: the feature
    strings are LLM-generated free-form text (schemas.py only enforces list[str]),
    so anything unparseable is skipped rather than raising.

    Example feature strings (prompts.py:20):
        "shell:thickness=2"
        "through_hole:diameter=3.2,count=4,pattern=rectangular,..."
        "fillet:radius=2,edges=all_vertical"
    """
    holes = 0
    if not features:
        return {"holes": 0}
    for feat in features:
        if not isinstance(feat, str) or ":" not in feat:
            # also handle a bare "hole" with no params
            if isinstance(feat, str) and _HOLE_FEATURE_RE.search(feat):
                holes += 1
            continue
        name, _, rest = feat.partition(":")
        if not _HOLE_FEATURE_RE.search(name):
            continue
        # find count=N; default 1 hole if the feature names a hole but omits count
        count = 1
        for token in rest.split(","):
            k, _, v = token.partition("=")
            if k.strip().lower() == "count":
                try:
                    count = int(float(v.strip()))
                except (ValueError, TypeError):
                    count = 1
                break
        holes += count
    return {"holes": holes}


def _bbox_extents(bbox) -> list[float] | None:
    """Return [dx, dy, dz] from a BoundingBox-like object, or None."""
    if bbox is None:
        return None
    try:
        return [
            bbox.x_max - bbox.x_min,
            bbox.y_max - bbox.y_min,
            bbox.z_max - bbox.z_min,
        ]
    except AttributeError:
        return None


def dims_match(bbox, expected: dict, tol: float = 0.10) -> bool | None:
    """Compare actual bbox extents to expected dims, orientation-independent.

    Mirrors benchmark/runner.py:62-82: sort both extent triples descending and
    compare each within relative `tol`. Returns None when there's nothing to
    compare (no expected dims, no bbox). Only expected values > 0 are checked.
    """
    if not expected:
        return None
    actual = _bbox_extents(bbox)
    if actual is None:
        return None
    actual_sorted = sorted(actual, reverse=True)
    expected_sorted = sorted(
        [expected.get("width", 0), expected.get("height", 0), expected.get("depth", 0)],
        reverse=True,
    )
    return all(
        abs(a - e) / max(e, 1) < tol
        for a, e in zip(actual_sorted, expected_sorted)
        if e > 0
    )


def _get_inspect(response):
    return getattr(response, "inspect_report", None)


def _get_validation(response):
    return getattr(response, "validation", None)


def _watertight(response) -> bool | None:
    insp = _get_inspect(response)
    if insp is not None:
        return getattr(insp, "is_watertight", None)
    val = _get_validation(response)
    if val is not None:
        return getattr(val, "is_watertight", None)
    return None


def _printable(response) -> bool | None:
    insp = _get_inspect(response)
    if insp is not None and getattr(insp, "printable", None) is not None:
        return insp.printable
    val = _get_validation(response)
    if val is not None:
        return getattr(val, "printable", None)
    return None


def _bbox(response):
    val = _get_validation(response)
    if val is not None and getattr(val, "bounding_box", None) is not None:
        return val.bounding_box
    insp = _get_inspect(response)
    if insp is not None:
        return getattr(insp, "bounding_box", None)
    return None


def _has_2d_output(response) -> bool:
    files = getattr(response, "files", None) or {}
    return any(k in files for k in ("dxf", "svg"))


def extract_metrics(response, case: dict, wall_time_ms: int | None = None) -> dict:
    """Extract one run's flat metric dict from a GenerateResponse + case spec.

    `response` may be None (e.g. the pipeline raised); that yields an all-failed
    metric row so a crash counts as a real failure, not a gap.
    """
    path = case.get("path", "extrude_cut")
    is_2d = path == "2d"
    tol = case.get("tol", 0.10)

    if response is None:
        return {
            "executed": False, "verdict": None, "verdict_pass": False,
            "watertight": None, "printable": None,
            "dim_match": None, "part_type_match": None, "feature_match": None,
            "dfm_score": None, "attempts": 0, "one_shot": False,
            "exec_time_ms": 0, "wall_time_ms": wall_time_ms or 0,
            "error": "pipeline raised / None response", "passed": False,
        }

    executed = bool(getattr(response, "success", False))
    insp = _get_inspect(response)
    verdict = getattr(insp, "verdict", None) if insp is not None else None
    verdict_pass = verdict == "pass"

    watertight = _watertight(response)
    printable = _printable(response)
    bbox = _bbox(response)

    # --- L2 intent ---
    dim_match = dims_match(bbox, case.get("expected_dims"), tol) if executed else None

    part_type_match = None
    exp_pt = case.get("expected_part_type")
    plan = getattr(response, "plan", None)
    if exp_pt and plan is not None:
        part_type_match = getattr(plan, "part_type", None) == exp_pt

    feature_match = None
    exp_feat = case.get("expected_features")
    if exp_feat and plan is not None:
        got = parse_feature_counts(getattr(plan, "features", None))
        # exact match on each requested key (currently only 'holes' supported)
        feature_match = all(got.get(k) == v for k, v in exp_feat.items())

    # --- L3 dfm ---
    dfm = getattr(response, "dfm_analysis", None)
    dfm_score = dfm.get("design_score") if isinstance(dfm, dict) else None

    # --- process ---
    attempts = int(getattr(response, "attempts", 0) or 0)
    one_shot = executed and attempts == 1
    exec_time_ms = int(getattr(response, "execution_time_ms", 0) or 0)
    err = getattr(response, "error", None)
    error = None if err is None else str(err)

    # `passed` is gated ONLY on actual geometry facts (executed + printable + dims),
    # all of which come from the produced STL / bbox and are reliable. part_type_match
    # and feature_match derive from the planner's free-form understanding (not the
    # geometry), so they are recorded as diagnostics but must NOT gate pass/fail —
    # otherwise a geometrically-correct model fails on a classification wobble.
    intent_ok = dim_match is not False

    if is_2d:
        passed = executed and _has_2d_output(response) and intent_ok
    else:
        should_print = case.get("should_be_printable", True)
        if should_print:
            printable_ok = printable is True
        else:
            # not a printability case: require a real measured solid (inspect present)
            printable_ok = insp is not None and verdict is not None
        passed = executed and printable_ok and intent_ok

    return {
        "executed": executed,
        "verdict": verdict,
        "verdict_pass": verdict_pass,
        "watertight": watertight,
        "printable": printable,
        "dim_match": dim_match,
        "part_type_match": part_type_match,
        "feature_match": feature_match,
        "dfm_score": dfm_score,
        "attempts": attempts,
        "one_shot": one_shot,
        "exec_time_ms": exec_time_ms,
        "wall_time_ms": wall_time_ms if wall_time_ms is not None else 0,
        "error": error,
        "passed": passed,
    }


# --- aggregation helpers ------------------------------------------------------

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def _rate_stats(bool_runs: list[bool]) -> dict:
    """mean/std of a boolean metric across runs (treating True=1)."""
    xs = [1.0 if b else 0.0 for b in bool_runs]
    return {"mean": round(_mean(xs), 4), "std": round(_std(xs), 4), "n": len(xs)}


def aggregate_case(runs: list[dict]) -> dict:
    """Aggregate the N per-run metric dicts of a single case.

    pass@1 = mean of `passed` across runs (expected single-shot success)
    pass@k = 1 if ANY run passed   (capability ceiling)
    pass^k = 1 if ALL runs passed  (stability)
    """
    passed = [r["passed"] for r in runs]
    return {
        "n_runs": len(runs),
        "pass@1": _rate_stats(passed),
        "pass@k": any(passed),
        "pass^k": all(passed) if passed else False,
        "verdict_pass_rate": _rate_stats([r["verdict_pass"] for r in runs]),
        "printable_rate": _rate_stats([r["printable"] is True for r in runs]),
        "watertight_rate": _rate_stats([r["watertight"] is True for r in runs]),
        "one_shot_rate": _rate_stats([r["one_shot"] for r in runs]),
        "dim_match_rate": _rate_stats([r["dim_match"] is True for r in runs]),
        "avg_attempts": round(_mean([r["attempts"] for r in runs]), 2),
        "avg_wall_ms": round(_mean([r["wall_time_ms"] for r in runs])),
    }


def aggregate_summary(cases: list[dict]) -> dict:
    """Aggregate across all cases. `cases` is a list of {id, path, difficulty,
    runs:[...], agg:{...}} entries. Produces overall + per-difficulty + per-path."""
    def _collect(entries: list[dict]) -> dict:
        # pass@1: mean over cases of each case's pass@1 mean
        p1 = [c["agg"]["pass@1"]["mean"] for c in entries]
        vp = [c["agg"]["verdict_pass_rate"]["mean"] for c in entries]
        pr = [c["agg"]["printable_rate"]["mean"] for c in entries]
        os_ = [c["agg"]["one_shot_rate"]["mean"] for c in entries]
        passk = [1.0 if c["agg"]["pass@k"] else 0.0 for c in entries]
        passhat = [1.0 if c["agg"]["pass^k"] else 0.0 for c in entries]
        wall = [c["agg"]["avg_wall_ms"] for c in entries]
        return {
            "n_cases": len(entries),
            "pass@1": {"mean": round(_mean(p1), 4), "std": round(_std(p1), 4)},
            "pass@k": round(_mean(passk), 4),
            "pass^k": round(_mean(passhat), 4),
            "verdict_pass_rate": {"mean": round(_mean(vp), 4), "std": round(_std(vp), 4)},
            "printable_rate": {"mean": round(_mean(pr), 4), "std": round(_std(pr), 4)},
            "one_shot_rate": {"mean": round(_mean(os_), 4), "std": round(_std(os_), 4)},
            "avg_wall_ms": round(_mean(wall)),
        }

    summary = {"overall": _collect(cases)}

    by_diff = {}
    for diff in ("simple", "moderate", "complex"):
        entries = [c for c in cases if c.get("difficulty") == diff]
        if entries:
            by_diff[diff] = _collect(entries)
    summary["by_difficulty"] = by_diff

    by_path = {}
    paths = sorted({c.get("path", "extrude_cut") for c in cases})
    for p in paths:
        entries = [c for c in cases if c.get("path", "extrude_cut") == p]
        if entries:
            by_path[p] = _collect(entries)
    summary["by_path"] = by_path

    return summary
