"""Accuracy and latency report for one local_eval run.

Reads the JSON that run_eval.py writes and reproduces both headline tables:
pass@1 overall and per difficulty tier, and the wall-clock / execution-time
distribution per case.

Two things are deliberately reported side by side, because quoting either alone
is misleading:

* ``wall_time_ms`` is the whole generation, measured under whatever concurrency
  the run used, so it includes time a case spent queued behind others. It is
  throughput-under-load, not isolated latency.
* ``exec_time_ms`` is only the CAD execution. The gap between them is model
  latency -- planning, code generation, and the visual loop's render / critique
  / patch round trips.

Usage:  python report_timing.py <report.json> [more.json ...]
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

_TIERS = ("simple", "moderate", "complex")


def _rows(report: dict) -> list[tuple[str, dict]]:
    return [
        (case_id, run)
        for case_id, runs in (report.get("runs") or {}).items()
        for run in runs
    ]


def _seconds(rows, key: str) -> list[float]:
    return [run[key] / 1000 for _, run in rows if run.get(key)]


def _describe(values: list[float]) -> tuple[float, float, float, float]:
    return (
        min(values),
        statistics.median(values),
        statistics.mean(values),
        max(values),
    )


def _rate(rows, key: str = "passed") -> float:
    if not rows:
        return 0.0
    return sum(1 for _, run in rows if run.get(key)) / len(rows)


def report(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = _rows(payload)
    if not rows:
        print(f"{path}: no runs recorded")
        return

    meta = payload.get("meta") or {}
    print("=" * 78)
    print(f"REPORT  {path.name}")
    print("=" * 78)
    if meta:
        print(
            f"  model={meta.get('model')}  vision={meta.get('vision_model')}  "
            f"visual_refinement={meta.get('visual_refinement_enabled')}  "
            f"repeats={meta.get('n_repeats')}"
        )
        print(f"  executor: {meta.get('executor')}")
    print()

    passed = sum(1 for _, run in rows if run.get("passed"))
    print(f"ACCURACY  pass@1 = {passed / len(rows):.1%}   ({passed}/{len(rows)})")
    print(f"  {'tier':<10} {'n':>3} {'pass@1':>8}")
    for tier in _TIERS:
        subset = [item for item in rows if item[1].get("difficulty") == tier]
        if subset:
            hits = sum(1 for _, run in subset if run.get("passed"))
            print(f"  {tier:<10} {len(subset):>3} {hits / len(subset):>7.1%}")
    print()

    for label, key in (
        ("WALL CLOCK per case (s)  [includes queueing under concurrency]", "wall_time_ms"),
        ("CAD EXECUTION only (s)", "exec_time_ms"),
    ):
        values = _seconds(rows, key)
        if not values:
            continue
        low, median, mean, high = _describe(values)
        print(f"{label}   n={len(values)}")
        print(
            f"  min {low:7.1f}   median {median:7.1f}   "
            f"mean {mean:7.1f}   max {high:7.1f}"
        )
        print()

    print("WALL CLOCK by difficulty (s)")
    print(f"  {'tier':<10} {'n':>3} {'min':>8} {'median':>8} {'mean':>8} {'max':>8}")
    for tier in _TIERS:
        subset = [item for item in rows if item[1].get("difficulty") == tier]
        values = _seconds(subset, "wall_time_ms")
        if values:
            low, median, mean, high = _describe(values)
            print(
                f"  {tier:<10} {len(values):>3} {low:8.1f} {median:8.1f} "
                f"{mean:8.1f} {high:8.1f}"
            )
    print()

    print("WALL CLOCK by outcome (s)")
    for label, wanted in (("passed", True), ("failed", False)):
        subset = [item for item in rows if bool(item[1].get("passed")) is wanted]
        values = _seconds(subset, "wall_time_ms")
        if values:
            low, median, mean, high = _describe(values)
            print(
                f"  {label:<10} {len(values):>3} {low:8.1f} {median:8.1f} "
                f"{mean:8.1f} {high:8.1f}"
            )
    print()

    ordered = sorted(rows, key=lambda item: -(item[1].get("wall_time_ms") or 0))
    slowest = ", ".join(
        f"{cid}={run['wall_time_ms'] / 1000:.0f}s" for cid, run in ordered[:5]
    )
    fastest = ", ".join(
        f"{cid}={run['wall_time_ms'] / 1000:.0f}s" for cid, run in ordered[-5:][::-1]
    )
    print(f"  slowest 5: {slowest}")
    print(f"  fastest 5: {fastest}")
    print()

    total = sum(run.get("wall_time_ms") or 0 for _, run in rows) / 1000
    print(
        f"  sum of per-case wall time: {total / 60:.1f} min "
        "(concurrent run, so real elapsed was lower)"
    )

    failures = [
        (cid, run) for cid, run in rows if not run.get("passed")
    ]
    if failures:
        print()
        print(f"FAILING CASES ({len(failures)}):")
        for cid, run in failures:
            reasons = []
            if not run.get("executed"):
                reasons.append("did not execute")
            elif run.get("printable") is not True and run.get("path") != "2d":
                reasons.append(f"printable={run.get('printable')}")
            if run.get("dim_match") is False:
                reasons.append("dimensions off")
            gate = run.get("artifact_gate") or {}
            if gate.get("passed") is False:
                reasons.append("artifact gate")
            print(
                f"  {cid:>4} {str(run.get('difficulty')):<9} "
                f"{'; '.join(reasons) or 'unknown'}"
            )
    print()


def main() -> int:
    paths = [Path(arg) for arg in sys.argv[1:]]
    if not paths:
        print(__doc__)
        return 2
    missing = [path for path in paths if not path.is_file()]
    if missing:
        for path in missing:
            print(f"not found: {path}")
        return 1
    for path in paths:
        report(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
