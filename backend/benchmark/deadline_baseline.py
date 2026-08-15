"""Derive a bounded product baseline from an existing real eval report.

The source report is preserved. Runs that only completed after the selected
whole-pipeline deadline are converted to truthful timeout failures, then all
aggregates are recomputed. No successful result is synthesized.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from benchmark import metrics as M


def _timeout_run(run: dict, path: str, deadline_s: float) -> dict:
    observed_wall_ms = int(run.get("wall_time_ms") or 0)
    deadline_label = f"{deadline_s:g}"
    artifact_gate = (
        {"dxf_readable": False, "passed": False}
        if path == "2d"
        else {
            "step_readable": False,
            "stl_readable": False,
            "geometry_nonempty": False,
            "rendered_views": 0,
            "passed": False,
        }
    )
    return {
        "executed": False,
        "verdict": None,
        "verdict_pass": False,
        "watertight": None,
        "printable": None,
        "dim_match": None,
        "part_type_match": None,
        "feature_match": None,
        "dfm_score": None,
        "attempts": 0,
        "one_shot": False,
        "exec_time_ms": 0,
        "wall_time_ms": round(deadline_s * 1000),
        "error": f"pipeline deadline exceeded after {deadline_label}s",
        "passed": False,
        "artifact_gate": artifact_gate,
        "rendered_views": 0,
        "request_id": None,
        "observed_post_deadline_wall_ms": observed_wall_ms,
        "observed_post_deadline_passed": bool(run.get("passed")),
        "observed_post_deadline_request_id": run.get("request_id"),
    }


def apply_pipeline_deadline(report: dict, deadline_s: float) -> dict:
    if deadline_s <= 0:
        raise ValueError("deadline_s must be greater than zero")

    bounded = copy.deepcopy(report)
    meta = bounded.setdefault("meta", {})
    source_run_id = str(meta.get("run_id") or "unknown")
    source_total_wall_s = meta.pop("total_wall_s", None)
    meta.update({
        "run_id": f"{source_run_id}_deadline-{deadline_s:g}s",
        "derived_from_run_id": source_run_id,
        "pipeline_deadline_s": float(deadline_s),
        "baseline_derivation": (
            "real source runs completing after the product pipeline deadline "
            "are counted as timeout failures"
        ),
    })
    if source_total_wall_s is not None:
        meta["observed_unbounded_total_wall_s"] = source_total_wall_s

    deadline_ms = deadline_s * 1000
    for case in bounded.get("cases", []):
        path = case.get("path", "extrude_cut")
        case["runs"] = [
            _timeout_run(run, path, deadline_s)
            if int(run.get("wall_time_ms") or 0) > deadline_ms
            else run
            for run in case.get("runs", [])
        ]
        case["agg"] = M.aggregate_case(case["runs"])

    bounded["summary"] = M.aggregate_summary(bounded.get("cases", []))
    return bounded


def main() -> None:
    parser = argparse.ArgumentParser(
        description="derive a product-deadline baseline from a real eval report"
    )
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--deadline-s", type=float, required=True)
    args = parser.parse_args()

    source = Path(args.source)
    output = Path(args.output)
    report = json.loads(source.read_text(encoding="utf-8"))
    bounded = apply_pipeline_deadline(report, args.deadline_s)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(bounded, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
