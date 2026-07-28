"""Compare two eval reports: baseline vs candidate.

Answers the only question that matters for iteration: did this change make things
better, do nothing, or break something — and WHERE.

Two outputs:
  1. Summary metric table (baseline -> candidate, with delta). A delta is marked
     "significant" only when it exceeds the baseline's pass@1 std — otherwise it's
     within run-to-run LLM noise and should not be read as a real change.
  2. Per-case regression list: which cases got FIXED, which BROKE, which are flaky,
     based on each case's pass@1 mean. This catches the side-effects of a prompt
     tweak that the aggregate hides (overall flat, but 3 cases broke and 3 fixed).

The release gate is intentionally strict. It returns a non-zero exit code for
non-comparable metadata or case sets, hard/net regressions, pass@1 below the
baseline noise floor, incomplete successful artifacts, or false success claims.
This makes the command suitable for CI and nightly release qualification.

Usage:
    cd backend && python -m benchmark.compare reports/baseline.json reports/candidate.json
"""
from __future__ import annotations

import argparse
import json
import sys

# A case's pass@1 mean must move by more than this to count as fixed/broken, on top
# of being a real (>0) move. Guards against 1/3 vs 1/3 noise being called a change.
CASE_DELTA_THRESHOLD = 0.34  # ~ one run out of three

_COMPARABLE_META_PATHS = (
    ("model", ("model",)),
    ("llm_provider", ("llm_provider",)),
    ("temperature", ("temperature",)),
    ("rag_enabled", ("rag_enabled",)),
    ("n_repeats", ("n_repeats",)),
    ("n_cases", ("n_cases",)),
    ("concurrency", ("concurrency",)),
    ("case_set_hash", ("case_set_hash",)),
    ("sandbox_runtime", ("sandbox_runtime",)),
    (
        "runtime_identity.image_digest",
        ("runtime_identity", "image_digest"),
    ),
    (
        "runtime_identity.architecture",
        ("runtime_identity", "architecture"),
    ),
)


def _overall(report: dict) -> dict:
    return report["summary"]["overall"]


def _case_pass1(report: dict) -> dict[str, float]:
    return {c["id"]: c["agg"]["pass@1"]["mean"] for c in report["cases"]}


def _nested(mapping: dict, path: tuple[str, ...]):
    value = mapping
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _metadata_mismatches(base: dict, cand: dict) -> list[dict]:
    base_meta = base.get("meta", {})
    cand_meta = cand.get("meta", {})
    mismatches = []
    for field, path in _COMPARABLE_META_PATHS:
        before = _nested(base_meta, path)
        after = _nested(cand_meta, path)
        if before != after:
            mismatches.append({
                "field": field,
                "base": before,
                "cand": after,
            })
    return mismatches


def _candidate_artifact_failures(report: dict) -> tuple[list[dict], list[dict]]:
    failures: list[dict] = []
    false_successes: list[dict] = []
    for case in report.get("cases", []):
        case_id = case.get("id", "?")
        path = case.get("path", "extrude_cut")
        for index, run in enumerate(case.get("runs", []), start=1):
            executed = run.get("executed") is True
            claimed_pass = run.get("passed") is True
            gate = run.get("artifact_gate")
            gate = gate if isinstance(gate, dict) else {}

            if claimed_pass and (not executed or gate.get("passed") is not True):
                false_successes.append({
                    "case_id": case_id,
                    "run": index,
                    "reason": "报告声称通过，但执行或产物门禁没有通过",
                })

            # Failed executions are already counted by pass@1. Artifact evidence is
            # mandatory only when the backend reported a successful execution.
            if not executed:
                continue

            if path == "2d":
                if gate.get("dxf_readable") is not True:
                    failures.append({
                        "case_id": case_id,
                        "run": index,
                        "reason": "DXF",
                    })
                continue

            checks = (
                ("step_readable", "STEP"),
                ("stl_readable", "STL"),
                ("geometry_nonempty", "非空几何"),
            )
            for key, reason in checks:
                if gate.get(key) is not True:
                    failures.append({
                        "case_id": case_id,
                        "run": index,
                        "reason": reason,
                    })
            if int(gate.get("rendered_views") or 0) < 4:
                failures.append({
                    "case_id": case_id,
                    "run": index,
                    "reason": "四视图",
                })
    return failures, false_successes


def compare_reports(base: dict, cand: dict) -> dict:
    bo, co = _overall(base), _overall(cand)

    def _delta(metric: str, nested: str | None = "mean") -> dict:
        b = bo[metric][nested] if nested else bo[metric]
        c = co[metric][nested] if nested else co[metric]
        return {"base": b, "cand": c, "delta": round(c - b, 4)}

    base_std = bo["pass@1"]["std"]
    summary = {
        "pass@1": _delta("pass@1"),
        "pass@k": _delta("pass@k", None),
        "pass^k": _delta("pass^k", None),
        "verdict_pass_rate": _delta("verdict_pass_rate"),
        "printable_rate": _delta("printable_rate"),
        "one_shot_rate": _delta("one_shot_rate"),
        "avg_wall_ms": _delta("avg_wall_ms", None),
    }
    # significance: pass@1 delta beyond baseline noise band
    summary["pass@1"]["significant"] = abs(summary["pass@1"]["delta"]) > max(base_std, 1e-9)

    # per-case regression analysis
    bp, cp = _case_pass1(base), _case_pass1(cand)
    common = sorted(set(bp) & set(cp))
    fixed, broke, flaky_or_same = [], [], []
    for cid in common:
        d = cp[cid] - bp[cid]
        if d > CASE_DELTA_THRESHOLD:
            fixed.append((cid, bp[cid], cp[cid]))
        elif d < -CASE_DELTA_THRESHOLD:
            broke.append((cid, bp[cid], cp[cid]))
        else:
            flaky_or_same.append((cid, bp[cid], cp[cid]))

    # hard regressions: was fully passing (1.0), now not
    hard_regressions = [(cid, bp[cid], cp[cid]) for cid in common
                        if bp[cid] >= 0.999 and cp[cid] < 0.999]

    only_base = sorted(set(bp) - set(cp))
    only_cand = sorted(set(cp) - set(bp))
    metadata_mismatches = _metadata_mismatches(base, cand)
    artifact_failures, false_successes = _candidate_artifact_failures(cand)
    pass1_floor = max(
        0.0,
        bo["pass@1"]["mean"] - bo["pass@1"]["std"],
    )
    pass1_floor_passed = co["pass@1"]["mean"] >= pass1_floor
    net = len(fixed) - len(broke)
    gate_failures = []
    if metadata_mismatches:
        gate_failures.append("comparison metadata mismatch")
    if only_base or only_cand:
        gate_failures.append("case IDs differ")
    if hard_regressions:
        gate_failures.append("hard per-case regression")
    if net < 0:
        gate_failures.append("net per-case regression")
    if not pass1_floor_passed:
        gate_failures.append("candidate pass@1 is below the baseline one-standard-deviation floor")
    if artifact_failures:
        gate_failures.append("successful execution has incomplete artifact evidence")
    if false_successes:
        gate_failures.append("false success detected")

    return {
        "summary": summary,
        "fixed": fixed,
        "broke": broke,
        "hard_regressions": hard_regressions,
        "n_common": len(common),
        "only_in_base": only_base,
        "only_in_cand": only_cand,
        "meta_base": base.get("meta", {}),
        "meta_cand": cand.get("meta", {}),
        "case_set_changed": base.get("meta", {}).get("case_set_hash")
                            != cand.get("meta", {}).get("case_set_hash"),
        "metadata_mismatches": metadata_mismatches,
        "artifact_failures": artifact_failures,
        "false_successes": false_successes,
        "pass1_floor": round(pass1_floor, 4),
        "pass1_floor_passed": pass1_floor_passed,
        "gate_failures": gate_failures,
        "gate_passed": not gate_failures,
    }


def print_comparison(diff: dict):
    mb, mc = diff["meta_base"], diff["meta_cand"]
    print(f"baseline : {mb.get('run_id','?')}  (model={mb.get('model')}, rag={mb.get('rag_enabled')})")
    print(f"candidate: {mc.get('run_id','?')}  (model={mc.get('model')}, rag={mc.get('rag_enabled')})")
    if diff["case_set_changed"]:
        print("\n⚠️  CASE SET HASH DIFFERS — reports are NOT apples-to-apples. "
              "Per-case diffs still valid for common ids; aggregate deltas suspect.")
    if mb.get("rag_enabled") != mc.get("rag_enabled"):
        print("⚠️  RAG setting differs between reports.")
    if diff["metadata_mismatches"]:
        print("\nComparison metadata mismatches:")
        for mismatch in diff["metadata_mismatches"]:
            print(
                f"  {mismatch['field']}: "
                f"{mismatch['base']!r} != {mismatch['cand']!r}"
            )

    print("\nMetric                  baseline   candidate   delta")
    print("-" * 56)
    for name, d in diff["summary"].items():
        if name == "avg_wall_ms":
            print(f"  {name:20s}  {d['base']:8.0f}   {d['cand']:8.0f}   {d['delta']:+.0f}ms")
        else:
            sig = ""
            if name == "pass@1" and d.get("significant"):
                sig = "  *significant*"
            print(f"  {name:20s}  {d['base']:7.1%}   {d['cand']:7.1%}   {d['delta']:+.1%}{sig}")

    print(f"\nPer-case (of {diff['n_common']} common, threshold ±{CASE_DELTA_THRESHOLD:.0%} pass@1):")
    print(f"  ✅ fixed : {len(diff['fixed'])}   ❌ broke: {len(diff['broke'])}")
    for cid, b, c in diff["broke"]:
        print(f"      BROKE  {cid}: {b:.0%} -> {c:.0%}")
    for cid, b, c in diff["fixed"]:
        print(f"      FIXED  {cid}: {b:.0%} -> {c:.0%}")
    if diff["hard_regressions"]:
        print(f"  🚨 hard regressions (was 100% passing, now not): {len(diff['hard_regressions'])}")
        for cid, b, c in diff["hard_regressions"]:
            print(f"      {cid}: {b:.0%} -> {c:.0%}")
    if diff["only_in_base"] or diff["only_in_cand"]:
        print(f"  (ids only in base: {diff['only_in_base']}; only in cand: {diff['only_in_cand']})")

    candidate_pass1 = diff["summary"]["pass@1"]["cand"]
    floor_status = "PASS" if diff["pass1_floor_passed"] else "FAIL"
    print(
        f"\npass@1 floor: candidate {candidate_pass1:.1%} "
        f">= {diff['pass1_floor']:.1%} [{floor_status}]"
    )
    if diff["artifact_failures"]:
        print(f"Artifact evidence failures: {len(diff['artifact_failures'])}")
        for failure in diff["artifact_failures"][:20]:
            print(
                f"  {failure['case_id']} run {failure['run']}: "
                f"{failure['reason']}"
            )
        if len(diff["artifact_failures"]) > 20:
            print(f"  ... {len(diff['artifact_failures']) - 20} more")
    if diff["false_successes"]:
        print(f"False successes: {len(diff['false_successes'])}")

    print()
    if diff["gate_passed"]:
        print("RELEASE GATE: PASS")
    else:
        print("RELEASE GATE: FAIL")
        for failure in diff["gate_failures"]:
            print(f"  - {failure}")


def main():
    ap = argparse.ArgumentParser(description="Compare two eval reports.")
    ap.add_argument("baseline", help="baseline report json")
    ap.add_argument("candidate", help="candidate report json")
    args = ap.parse_args()

    with open(args.baseline, encoding="utf-8") as f:
        base = json.load(f)
    with open(args.candidate, encoding="utf-8") as f:
        cand = json.load(f)

    diff = compare_reports(base, cand)
    print_comparison(diff)

    sys.exit(0 if diff["gate_passed"] else 1)


if __name__ == "__main__":
    main()
