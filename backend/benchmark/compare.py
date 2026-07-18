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

Exit code is non-zero when there are net regressions (broken > fixed) OR any case
dropped from passing to failing — so this can gate a nightly run.

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


def _overall(report: dict) -> dict:
    return report["summary"]["overall"]


def _case_pass1(report: dict) -> dict[str, float]:
    return {c["id"]: c["agg"]["pass@1"]["mean"] for c in report["cases"]}


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

    # verdict
    net = len(diff["fixed"]) - len(diff["broke"])
    print()
    if diff["broke"] or diff["hard_regressions"]:
        if net < 0:
            print(f"VERDICT: net regression ({len(diff['broke'])} broke vs {len(diff['fixed'])} fixed).")
        else:
            print(f"VERDICT: mixed — {len(diff['broke'])} broke, {len(diff['fixed'])} fixed. Review broken cases.")
    elif diff["fixed"]:
        print(f"VERDICT: improvement ({len(diff['fixed'])} fixed, 0 broke).")
    else:
        print("VERDICT: no significant per-case change (within noise).")


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

    # non-zero exit on net regression or any hard regression (gate-friendly)
    net = len(diff["fixed"]) - len(diff["broke"])
    sys.exit(1 if (diff["hard_regressions"] or net < 0) else 0)


if __name__ == "__main__":
    main()
