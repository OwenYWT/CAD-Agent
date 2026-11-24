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


def _solid_count(response) -> int | None:
    """Read an explicit solid count when an authoritative response exposes it."""
    for source in (_get_inspect(response), _get_validation(response)):
        value = getattr(source, "solid_count", None) if source is not None else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _valid_solid(response) -> bool | None:
    """Read only an explicitly named valid-solid check, never an aggregate verdict."""
    for source in (_get_inspect(response), _get_validation(response)):
        if source is None:
            continue
        value = getattr(source, "valid_solid", None)
        if isinstance(value, bool):
            return value
        for check in getattr(source, "checks", ()) or ():
            name = str(getattr(check, "name", "")).casefold()
            if name in {"valid_solid", "geometry_valid", "solid_valid"}:
                status = str(getattr(check, "status", "")).casefold()
                if status in {"pass", "fail"}:
                    return status == "pass"
    return None


def _evidence(response) -> dict:
    bbox = _bbox(response)
    extents = _bbox_extents(bbox)
    solid_count = _solid_count(response)
    valid_solid = _valid_solid(response)
    return {
        "bbox_extents_mm": extents,
        "watertight": _watertight(response),
        "printable": _printable(response),
        "solid_count": solid_count,
        "valid_solid": valid_solid,
        "authority": {
            "bbox_extents_mm": "independent_geometry" if extents is not None else "unavailable",
            "watertight": "independent_geometry" if _watertight(response) is not None else "unavailable",
            "printable": "independent_geometry" if _printable(response) is not None else "unavailable",
            "solid_count": "independent_geometry" if solid_count is not None else "unavailable",
            "valid_solid": "independent_geometry" if valid_solid is not None else "unavailable",
        },
    }


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
            "executed": False, "verdict": None, "verdict_pass": None,
            "watertight": None, "printable": None,
            "dim_match": None, "part_type_match": None, "feature_match": None,
            "dfm_score": None, "attempts": 0, "one_shot": None,
            "exec_time_ms": 0, "wall_time_ms": wall_time_ms or 0,
            "error": "pipeline raised / None response", "passed": False,
            "evidence": {
                "bbox_extents_mm": None,
                "watertight": None,
                "printable": None,
                "solid_count": None,
                "valid_solid": None,
                "authority": {},
            },
        }

    executed = bool(getattr(response, "success", False))
    insp = _get_inspect(response)
    verdict = getattr(insp, "verdict", None) if insp is not None else None
    verdict_pass = verdict == "pass" if verdict is not None else None

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
    one_shot = executed and attempts == 1 if attempts > 0 else None
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
        "evidence": _evidence(response),
    }


def apply_artifact_gate(row: dict, case: dict, evidence: dict) -> dict:
    """Require materialized, parseable artifacts before a run can pass.

    The semantic/geometry metrics and the artifact checks are intentionally
    separate: the former evaluates the design, while this gate prevents a
    successful response with missing or corrupt deliverables from being counted
    as a product success.
    """
    normalized = dict(evidence)
    if case.get("path") == "2d":
        gate_passed = normalized.get("dxf_readable") is True
    else:
        gate_passed = (
            normalized.get("step_readable") is True
            and normalized.get("stl_readable") is True
            and normalized.get("geometry_nonempty") is True
            and int(normalized.get("rendered_views") or 0) >= 4
        )

    normalized["passed"] = gate_passed
    gated = dict(row)
    gated["artifact_gate"] = normalized
    gated["passed"] = bool(row.get("passed")) and gate_passed
    return gated


# --- aggregation helpers ------------------------------------------------------

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def _rate_stats(bool_runs: list[bool | None]) -> dict:
    """Aggregate a nullable boolean metric without treating missing as false."""
    values = [value for value in bool_runs if isinstance(value, bool)]
    xs = [1.0 if value else 0.0 for value in values]
    return {
        "mean": round(_mean(xs), 4) if xs else None,
        "std": round(_std(xs), 4) if xs else None,
        "n": len(xs),
        "total": len(bool_runs),
    }


def _status_counts(values: list[str], expected: tuple[str, ...]) -> dict:
    counts = {status: 0 for status in expected}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    counts["total"] = len(values)
    return counts


def _final_gate_status(row: dict) -> str:
    if row.get("passed") is True:
        return "passed"
    if row.get("executed") is True:
        return "failed"
    if row.get("executed") is False:
        return "blocked"
    return "unavailable"


def _cad_test_status(row: dict) -> str:
    cad_test = row.get("cad_test")
    if not isinstance(cad_test, dict):
        return "unavailable"
    status = str(cad_test.get("status") or "").casefold()
    if status in {"passed", "pass", "succeeded"}:
        return "passed"
    if status in {"failed", "fail", "error"}:
        return "failed"
    if status in {"not_applicable", "not applicable", "skipped"}:
        return "not_applicable"
    return "unavailable"


def _process_status(row: dict) -> str:
    process = row.get("process_evaluation")
    if not isinstance(process, dict):
        return "unavailable"
    status = str(process.get("status") or "").casefold()
    if status in {"measured", "partial", "unavailable"}:
        return status
    return "unavailable"


def _optional_metric(row: dict, key: str) -> bool | None:
    value = row.get(key)
    if not isinstance(value, bool):
        return None
    if key == "verdict_pass" and row.get("verdict") is None:
        return None
    if key == "one_shot" and int(row.get("attempts") or 0) <= 0:
        return None
    return value


def aggregate_case(runs: list[dict]) -> dict:
    """Aggregate the N per-run metric dicts of a single case.

    pass@1 = mean of `passed` across runs (expected single-shot success)
    pass@k = 1 if ANY run passed   (capability ceiling)
    pass^k = 1 if ALL runs passed  (stability)
    """
    passed = [r["passed"] for r in runs]
    process_rows = [
        r.get("process_evaluation")
        for r in runs
        if isinstance(r.get("process_evaluation"), dict)
    ]

    def _process_mean(key: str) -> float:
        values = [
            float(item[key])
            for item in process_rows
            if isinstance(item.get(key), (int, float))
            and not isinstance(item.get(key), bool)
        ]
        return round(_mean(values), 2) if values else 0.0

    def _process_rate(key: str) -> float:
        values = [item.get(key) == "passed" for item in process_rows]
        return round(_mean([1.0 if value else 0.0 for value in values]), 4) if values else 0.0

    process_evaluation = {
        "measured_rate": round(
            _mean([1.0 if item.get("status") == "measured" else 0.0 for item in process_rows]),
            4,
        )
        if process_rows
        else 0.0,
        "avg_workflow_activity_count": _process_mean("workflow_activity_count"),
        "avg_cad_operation_count": _process_mean("cad_operation_count"),
        "avg_repair_count": _process_mean("repair_count"),
        "avg_retry_call_count": _process_mean("retry_call_count"),
        "avg_failed_call_count": _process_mean("failed_call_count"),
        "avg_total_call_count": _process_mean("total_call_count"),
        "avg_activity_duration_ms": _process_mean("activity_duration_ms"),
        "geometry_pass_rate": _process_rate("geometry_status"),
        "dfm_pass_rate": _process_rate("dfm_status"),
        "visual_pass_rate": _process_rate("visual_status"),
        "duration_measured_rate": round(
            _mean([1.0 if item.get("duration_status") == "measured" else 0.0 for item in process_rows]),
            4,
        )
        if process_rows
        else 0.0,
        "repair_gain_measured_rate": round(
            _mean([1.0 if item.get("repair_gain_status") == "measured" else 0.0 for item in process_rows]),
            4,
        )
        if process_rows
        else 0.0,
    }

    return {
        "n_runs": len(runs),
        "pass@1": _rate_stats(passed),
        "pass@k": any(passed),
        "pass^k": all(passed) if passed else False,
        "verdict_pass_rate": _rate_stats(
            [_optional_metric(r, "verdict_pass") for r in runs]
        ),
        "printable_rate": _rate_stats(
            [_optional_metric(r, "printable") for r in runs]
        ),
        "watertight_rate": _rate_stats(
            [_optional_metric(r, "watertight") for r in runs]
        ),
        "one_shot_rate": _rate_stats(
            [_optional_metric(r, "one_shot") for r in runs]
        ),
        "dim_match_rate": _rate_stats(
            [_optional_metric(r, "dim_match") for r in runs]
        ),
        "avg_attempts": round(_mean([r.get("attempts", 0) for r in runs]), 2),
        "avg_wall_ms": round(_mean([r["wall_time_ms"] for r in runs])),
        "final_gate_status": _status_counts(
            [_final_gate_status(r) for r in runs],
            ("passed", "failed", "blocked", "unavailable", "not_applicable"),
        ),
        "cad_test_status": _status_counts(
            [_cad_test_status(r) for r in runs],
            ("passed", "failed", "blocked", "unavailable", "not_applicable"),
        ),
        "process_evaluation_status": _status_counts(
            [_process_status(r) for r in runs],
            ("measured", "partial", "unavailable"),
        ),
        "process_evaluation": process_evaluation,
    }


def aggregate_summary(cases: list[dict]) -> dict:
    """Aggregate across all cases. `cases` is a list of {id, path, difficulty,
    runs:[...], agg:{...}} entries. Produces overall + per-difficulty + per-path."""
    def _collect(entries: list[dict]) -> dict:
        # pass@1: mean over cases of each case's pass@1 mean
        p1 = [c["agg"]["pass@1"]["mean"] for c in entries]
        passk = [1.0 if c["agg"]["pass@k"] else 0.0 for c in entries]
        passhat = [1.0 if c["agg"]["pass^k"] else 0.0 for c in entries]
        wall = [c["agg"]["avg_wall_ms"] for c in entries]
        process = [c["agg"].get("process_evaluation") or {} for c in entries]

        def _process_summary(key: str) -> float:
            return round(
                _mean(
                    [
                        float(item[key])
                        for item in process
                        if isinstance(item.get(key), (int, float))
                        and not isinstance(item.get(key), bool)
                    ]
                ),
                4,
            )

        def _rate_summary(key: str) -> dict:
            values = [
                c["agg"][key]["mean"]
                for c in entries
                if c["agg"][key]["mean"] is not None
            ]
            return {
                "mean": round(_mean(values), 4) if values else None,
                "std": round(_std(values), 4) if values else None,
                "n": sum(c["agg"][key].get("n", 0) for c in entries),
                "total": sum(c["agg"][key].get("total", 0) for c in entries),
            }

        def _merge_status(key: str) -> dict:
            merged: dict[str, int] = {}
            for case in entries:
                for status, count in case["agg"].get(key, {}).items():
                    if status != "total":
                        merged[status] = merged.get(status, 0) + int(count)
            merged["total"] = sum(
                int(case["agg"].get(key, {}).get("total", 0)) for case in entries
            )
            return merged

        return {
            "n_cases": len(entries),
            "pass@1": {"mean": round(_mean(p1), 4), "std": round(_std(p1), 4)},
            "pass@k": round(_mean(passk), 4),
            "pass^k": round(_mean(passhat), 4),
            "verdict_pass_rate": _rate_summary("verdict_pass_rate"),
            "printable_rate": _rate_summary("printable_rate"),
            "one_shot_rate": _rate_summary("one_shot_rate"),
            "final_gate_status": _merge_status("final_gate_status"),
            "cad_test_status": _merge_status("cad_test_status"),
            "process_evaluation_status": _merge_status("process_evaluation_status"),
            "avg_wall_ms": round(_mean(wall)),
            "process_evaluation": {
                "measured_rate": _process_summary("measured_rate"),
                "avg_workflow_activity_count": _process_summary(
                    "avg_workflow_activity_count"
                ),
                "avg_cad_operation_count": _process_summary("avg_cad_operation_count"),
                "avg_repair_count": _process_summary("avg_repair_count"),
                "avg_retry_call_count": _process_summary("avg_retry_call_count"),
                "avg_failed_call_count": _process_summary("avg_failed_call_count"),
                "avg_total_call_count": _process_summary("avg_total_call_count"),
                "avg_activity_duration_ms": _process_summary("avg_activity_duration_ms"),
                "geometry_pass_rate": _process_summary("geometry_pass_rate"),
                "dfm_pass_rate": _process_summary("dfm_pass_rate"),
                "visual_pass_rate": _process_summary("visual_pass_rate"),
                "duration_measured_rate": _process_summary("duration_measured_rate"),
                "repair_gain_measured_rate": _process_summary("repair_gain_measured_rate"),
            },
        }

    summary = {"overall": _collect(cases)}

    by_diff = {}
    difficulties = sorted(
        {str(case.get("difficulty") or "unknown") for case in cases}
    )
    for diff in difficulties:
        entries = [
            c for c in cases if str(c.get("difficulty") or "unknown") == diff
        ]
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
