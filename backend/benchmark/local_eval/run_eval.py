"""Full benchmark run over every repository eval case, at every difficulty.

Real planner, real code generation, real CadQuery kernel, real geometry
validation, real vision gate, and the repository's own scoring functions
(benchmark.metrics.extract_metrics + apply_artifact_gate) plus its own artifact
verification helpers (benchmark.eval._verify_*).

The ONLY substitution is the isolation boundary: on a host with no container
runtime, generated code runs through LocalCadQueryExecutor instead of the
sandbox image. Nothing about the design, the prompts or the scoring is changed
for the measurement. Where a container runtime exists, ``python -m
benchmark.eval`` is the real harness and exercises the product's own executor.

Case expectations (expected_dims / expected_features) are used for SCORING ONLY
and are never shown to the pipeline; the pipeline sees the description alone,
exactly as a user request would arrive.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from local_executor import LocalCadQueryExecutor  # noqa: E402

# Windows' default Proactor loop closes transports during interpreter teardown
# and prints a "RuntimeError: Event loop is closed" traceback per pooled HTTPS
# connection. It is pure shutdown noise -- the run has already finished -- but it
# buries the report, so use the selector loop instead.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=1, help="repeats per case")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--cases", default="all")
    parser.add_argument("--difficulty", default=None)
    parser.add_argument("--out", default="eval_local_report.json")
    parser.add_argument(
        "--no-visual",
        action="store_true",
        help="disable the VLM refinement loop (ablation)",
    )
    args = parser.parse_args()

    from app.config import settings

    if args.no_visual:
        settings.visual_refinement_enabled = False

    from benchmark import metrics as M
    from benchmark.eval import _artifact_evidence, _render, _select_cases
    from app.agent.orchestrator import Orchestrator
    from app.rendering.renderer import CADRenderer

    # benchmark.eval installs INFO logging on import; the per-case result line is
    # the signal here, so keep everything below a warning quiet.
    import logging

    logging.getLogger().setLevel(logging.WARNING)
    for noisy in ("httpx", "openai", "app", "urllib3", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    cases = _select_cases(args.cases, args.difficulty)
    print(f"cases: {len(cases)}   repeats: {args.n}")
    print(f"model: {settings.llm_model}   vision: {settings.effective_vision_model}")
    print(f"visual refinement: {settings.visual_refinement_enabled} "
          f"(max {settings.visual_refinement_max_iterations} iterations)")
    print(f"deadline per generation: {settings.generate_deadline_s}s")
    print("-" * 78, flush=True)

    orchestrator = Orchestrator()
    executor = LocalCadQueryExecutor()
    orchestrator.executor = executor  # the only substituted component

    renderer = CADRenderer()
    run_root = Path(args.out).with_suffix("")
    run_root.mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(args.concurrency)
    rows: dict[str, list] = defaultdict(list)
    done = {"n": 0}
    total = len(cases) * args.n

    async def run_one(case: dict, index: int) -> None:
        async with semaphore:
            started = time.time()
            response = None
            failure = None
            try:
                response = await asyncio.wait_for(
                    orchestrator.generate(
                        case["description"], output_formats=["step", "stl"]
                    ),
                    timeout=settings.generate_deadline_s,
                )
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
            wall_ms = int((time.time() - started) * 1000)

            request_id = getattr(response, "request_id", None)
            rendered = 0
            if response is not None and getattr(response, "success", False):
                rendered = await asyncio.to_thread(
                    _render, renderer, request_id, run_root / f"{case['id']}_run{index}"
                )
            try:
                evidence = await _artifact_evidence(
                    executor, request_id, case.get("path", "extrude_cut"), rendered
                )
            except Exception as exc:
                evidence = {"evidence_error": f"{type(exc).__name__}: {exc}"}

            row = M.extract_metrics(response, case, wall_time_ms=wall_ms)
            row = M.apply_artifact_gate(row, case, evidence)
            if failure:
                row["error"] = failure
            row["difficulty"] = case["difficulty"]
            row["path"] = case.get("path")
            rows[case["id"]].append(row)

            done["n"] += 1
            mark = "PASS" if row["passed"] else "fail"
            print(
                f"  [{done['n']:>3}/{total}] {case['id']:>4} {case['difficulty']:<9} "
                f"{mark}  exec={row['executed']} print={row['printable']} "
                f"dim={row['dim_match']} {wall_ms/1000:.0f}s "
                f"{(row.get('error') or '')[:60]}",
                flush=True,
            )

    await asyncio.gather(
        *[run_one(case, i) for case in cases for i in range(args.n)]
    )

    # --- aggregate exactly as the repo does ---
    by_case = {cid: M.aggregate_case(runs) for cid, runs in rows.items()}
    flat = [r for runs in rows.values() for r in runs]

    def rate(items, key="passed"):
        return (sum(1 for r in items if r[key]) / len(items)) if items else 0.0

    overall = rate(flat)
    print()
    print("=" * 78)
    print(f"OVERALL pass@1 : {overall:.1%}  ({sum(1 for r in flat if r['passed'])}/{len(flat)})")
    print("=" * 78)
    by_difficulty = {}
    for difficulty in ("simple", "moderate", "complex"):
        subset = [r for r in flat if r["difficulty"] == difficulty]
        if subset:
            by_difficulty[difficulty] = rate(subset)
            print(
                f"  {difficulty:<9}: {rate(subset):.1%}  "
                f"({sum(1 for r in subset if r['passed'])}/{len(subset)})"
            )
    print()
    print("  executed        :", f"{rate(flat, 'executed'):.1%}")
    printable = [r for r in flat if r["printable"] is not None]
    print("  printable       :", f"{rate(printable, 'printable'):.1%}" if printable else "n/a")
    dims = [r for r in flat if r["dim_match"] is not None]
    print("  dim_match       :", f"{rate(dims, 'dim_match'):.1%}" if dims else "n/a")

    failures = [
        (cid, runs[0])
        for cid, runs in rows.items()
        if not all(r["passed"] for r in runs)
    ]
    if failures:
        print()
        print(f"FAILING CASES ({len(failures)}):")
        for cid, row in failures:
            reason = []
            if not row["executed"]:
                reason.append("did not execute")
            elif row["printable"] is not True and row.get("path") != "2d":
                reason.append(f"printable={row['printable']}")
            if row["dim_match"] is False:
                reason.append("dims off")
            gate = row.get("artifact_gate") or {}
            if gate.get("passed") is False:
                reason.append(f"artifact gate {gate}")
            print(f"  {cid:>4} {row['difficulty']:<9} {'; '.join(reason) or 'unknown'}")
            if row.get("error"):
                print(f"        {str(row['error'])[:150]}")

    Path(args.out).write_text(
        json.dumps(
            {
                "meta": {
                    "model": settings.llm_model,
                    "vision_model": settings.effective_vision_model,
                    "visual_refinement_enabled": settings.visual_refinement_enabled,
                    "visual_refinement_max_iterations": (
                        settings.visual_refinement_max_iterations
                    ),
                    "n_repeats": args.n,
                    "n_cases": len(cases),
                    "executor": "LocalCadQueryExecutor (no container runtime on host)",
                },
                "summary": {
                    "pass@1": overall,
                    "by_difficulty": by_difficulty,
                },
                "by_case": by_case,
                "runs": {cid: runs for cid, runs in rows.items()},
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"\nreport written to {args.out}")


asyncio.run(main())
