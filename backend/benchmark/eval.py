"""Standard evaluation harness for the CAD Agent.

Runs the REAL pipeline (real LLM + real Docker/Podman sandbox) over the 50-case
eval set, N times each, and emits a versioned, comparable report:

  - layered metrics per run (see benchmark/metrics.py)
  - aggregation per case (pass@1 / pass@k / pass^k) and overall / by-difficulty / by-path
  - rendered 4-view PNGs per run for human spot-checking of shape correctness
  - report metadata (git sha, model, temperature, rag on/off, case-set hash) so two
    reports are only ever compared apples-to-apples

This is the EXPENSIVE, PROBABILISTIC eval. Do NOT put it in PR CI — run it manually
or nightly when prompts / model / retriever changed. The hermetic logic-regression
suite (tests/) stays in CI.

Usage:
    cd backend && python -m benchmark.eval \
        [--n 3] [--cases all|P01,P02,...] [--no-rag] \
        [--concurrency 4] [--baseline reports/eval_xxx.json] [--no-render]

Requires real LLM credentials (see config) and the sandbox image
(cad-agent-sandbox:latest). Wall-clock: ~20-50 min for 50x3 with concurrency.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import platform
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from benchmark import metrics as M
from benchmark.eval_cases import EVAL_CASES, EVAL_CASES_BY_ID, case_set_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("eval")

REPORTS_DIR = Path(__file__).parent / "reports"


class _EmptyRetriever:
    """Drop-in for the retriever that returns no examples — used by --no-rag to
    measure zero-shot capability (model + prompt only, no RAG)."""
    async def find_similar(self, query: str, top_k: int = 3) -> list[dict]:
        return []


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[2]),
        )
        sha = out.stdout.strip()
        # mark dirty working tree so a baseline from uncommitted code is identifiable
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[2]),
        )
        if dirty.stdout.strip():
            sha += "-dirty"
        return sha or "unknown"
    except Exception:
        return "unknown"


def _find_stl(request_id: str | None) -> Path | None:
    return _find_artifact(request_id, ".stl")


def _find_artifact(request_id: str | None, suffix: str) -> Path | None:
    if not request_id:
        return None
    d = Path(settings.file_storage_dir) / request_id
    if not d.exists():
        return None
    matches = sorted(d.glob(f"*{suffix}"))
    return matches[0] if matches else None


def _render(renderer, request_id: str | None, out_dir: Path) -> int:
    """Render 4 views of the produced STL for human spot-check. Best-effort:
    returns count of images written (0 if no STL / render unavailable)."""
    stl = _find_stl(request_id)
    if stl is None:
        return 0
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = renderer.render_stl(stl, out_dir)
        return len(paths)
    except Exception as e:
        logger.warning(f"render failed for {request_id}: {e}")
        return 0


def _verify_stl(path: Path | None) -> dict:
    if path is None:
        return {"stl_readable": False, "geometry_nonempty": False}
    try:
        import trimesh

        loaded = trimesh.load(path, force="scene")
        geometries = list(loaded.geometry.values())
        nonempty = any(
            len(getattr(mesh, "vertices", [])) > 0
            and len(getattr(mesh, "faces", [])) > 0
            for mesh in geometries
        )
        return {"stl_readable": True, "geometry_nonempty": nonempty}
    except Exception as exc:
        return {
            "stl_readable": False,
            "geometry_nonempty": False,
            "stl_error": f"{type(exc).__name__}: {exc}",
        }


def _verify_dxf(path: Path | None) -> dict:
    if path is None:
        return {"dxf_readable": False}
    try:
        import ezdxf

        document = ezdxf.readfile(path)
        return {"dxf_readable": len(document.modelspace()) > 0}
    except Exception as exc:
        return {
            "dxf_readable": False,
            "dxf_error": f"{type(exc).__name__}: {exc}",
        }


async def _verify_step(executor, path: Path | None) -> dict:
    if path is None:
        return {"step_readable": False}

    result = await executor.execute(
        "result = cq.importers.importStep('/sandbox/input/source.step')",
        mode="3d",
        extra_files={"source.step": path},
    )
    try:
        readable = result.success and any(
            output.suffix.lower() == ".step" for output in result.files.values()
        )
        evidence = {"step_readable": readable}
        if not readable:
            evidence["step_error"] = (
                f"{result.error_type or 'ArtifactError'}: "
                f"{result.error_message or 'STEP re-import produced no STEP output'}"
            )
        return evidence
    finally:
        shutil.rmtree(result.work_dir, ignore_errors=True)


async def _artifact_evidence(executor, request_id: str | None, path: str, rendered: int) -> dict:
    if path == "2d":
        return await asyncio.to_thread(_verify_dxf, _find_artifact(request_id, ".dxf"))

    stl_evidence, step_evidence = await asyncio.gather(
        asyncio.to_thread(_verify_stl, _find_artifact(request_id, ".stl")),
        _verify_step(executor, _find_artifact(request_id, ".step")),
    )
    return {**stl_evidence, **step_evidence, "rendered_views": rendered}


def _runtime_identity() -> dict:
    runtime = settings.sandbox_runtime.strip().lower()
    command = "podman" if runtime == "podman" else "docker"
    identity = {
        "runtime": runtime,
        "image": settings.sandbox_image,
        "host_platform": f"{platform.system().lower()}/{platform.machine().lower()}",
    }
    try:
        result = subprocess.run(
            [command, "image", "inspect", settings.sandbox_image],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            identity["inspect_error"] = (result.stderr or result.stdout).strip()
            return identity
        inspected = json.loads(result.stdout)[0]
        image_id = str(inspected.get("Id") or inspected.get("ID") or "")
        if image_id and not image_id.startswith("sha256:"):
            image_id = f"sha256:{image_id}"
        identity.update(
            {
                "image_digest": image_id or None,
                "source_digest": inspected.get("Digest"),
                "architecture": inspected.get("Architecture"),
                "os": inspected.get("Os"),
            }
        )
    except Exception as exc:
        identity["inspect_error"] = f"{type(exc).__name__}: {exc}"
    return identity


async def run_eval(
    cases: list[dict], n: int, concurrency: int, use_rag: bool, do_render: bool,
    run_dir: Path,
) -> dict:
    from app.agent.orchestrator import Orchestrator

    orch = Orchestrator()  # builds real planner/codegen/executor/validators/retriever
    if not use_rag:
        orch.retriever = _EmptyRetriever()
        logger.info("RAG disabled (zero-shot mode)")

    renderer = orch.renderer
    sem = asyncio.Semaphore(concurrency)

    # one work item per (case, repeat)
    work = [(case, i) for case in cases for i in range(n)]
    results: dict[str, list] = {c["id"]: [None] * n for c in cases}

    async def _one(case: dict, rep: int):
        async with sem:
            cid = case["id"]
            logger.info(f"[{cid} run {rep+1}/{n}] {case['description'][:40]}")
            t0 = time.time()
            response = None
            try:
                response = await orch.generate(case["description"], output_formats=["step", "stl"])
            except Exception as e:
                logger.error(f"[{cid} run {rep+1}] pipeline raised: {e}")
            wall_ms = int((time.time() - t0) * 1000)

            row = M.extract_metrics(response, case, wall_time_ms=wall_ms)

            # render for human spot-check (best-effort, doesn't affect metrics)
            rendered = 0
            if do_render and response is not None and getattr(response, "success", False):
                rid = getattr(response, "request_id", None)
                rdir = run_dir / "renders" / f"{cid}_run{rep+1}"
                rendered = await asyncio.get_event_loop().run_in_executor(
                    None, _render, renderer, rid, rdir
                )
            request_id = getattr(response, "request_id", None) if response else None
            evidence = await _artifact_evidence(
                orch.executor,
                request_id,
                case.get("path", "extrude_cut"),
                rendered,
            )
            row = M.apply_artifact_gate(row, case, evidence)
            row["rendered_views"] = rendered
            row["request_id"] = request_id

            status = "PASS" if row["passed"] else "fail"
            logger.info(f"[{cid} run {rep+1}] {status} "
                        f"(verdict={row['verdict']}, attempts={row['attempts']}, {wall_ms}ms, imgs={rendered})")
            results[cid][rep] = row

    await asyncio.gather(*[_one(c, i) for (c, i) in work])

    # assemble per-case + summary
    case_entries = []
    for case in cases:
        runs = results[case["id"]]
        case_entries.append({
            "id": case["id"],
            "difficulty": case["difficulty"],
            "path": case["path"],
            "description": case["description"],
            "runs": runs,
            "agg": M.aggregate_case(runs),
        })

    summary = M.aggregate_summary(case_entries)
    return {"summary": summary, "cases": case_entries}


def _select_cases(spec: str) -> list[dict]:
    if spec == "all":
        return EVAL_CASES
    ids = [s.strip() for s in spec.split(",") if s.strip()]
    out = []
    for i in ids:
        if i not in EVAL_CASES_BY_ID:
            raise SystemExit(f"unknown case id: {i}")
        out.append(EVAL_CASES_BY_ID[i])
    return out


def main():
    ap = argparse.ArgumentParser(description="CAD Agent standard eval harness.")
    ap.add_argument("--n", type=int, default=3, help="repeats per case (default 3)")
    ap.add_argument("--cases", default="all", help="'all' or comma-separated ids (P01,X01)")
    ap.add_argument("--no-rag", action="store_true", help="disable retriever (zero-shot)")
    ap.add_argument("--concurrency", type=int, default=4, help="parallel generations (default 4)")
    ap.add_argument("--no-render", action="store_true", help="skip render PNGs (faster)")
    ap.add_argument("--baseline", default=None, help="baseline report to diff against after run")
    args = ap.parse_args()

    if not settings.has_llm_credentials:
        raise SystemExit(f"LLM credentials missing: {settings.llm_credentials_error}")

    cases = _select_cases(args.cases)
    sha = _git_sha()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"eval_{ts}_{sha}"
    run_dir = REPORTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "run_id": run_id,
        "timestamp": ts,
        "git_sha": sha,
        "model": settings.llm_model,
        "llm_provider": settings.normalized_llm_provider,
        "temperature": 0.2,  # codegen default (code_gen.py); planner uses 0.1
        "rag_enabled": not args.no_rag,
        "n_repeats": args.n,
        "concurrency": args.concurrency,
        "n_cases": len(cases),
        "case_set_hash": case_set_hash(),
        "sandbox_runtime": settings.sandbox_runtime,
        "runtime_identity": _runtime_identity(),
    }

    logger.info(f"Starting {run_id}: {len(cases)} cases x{args.n} "
                f"(rag={'on' if not args.no_rag else 'off'}, conc={args.concurrency})")

    t0 = time.time()
    payload = asyncio.run(run_eval(
        cases, args.n, args.concurrency, not args.no_rag, not args.no_render, run_dir,
    ))
    meta["total_wall_s"] = round(time.time() - t0, 1)

    report = {"meta": meta, **payload}
    report_path = run_dir / "report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # also drop a top-level copy at reports/<run_id>.json for easy reference
    flat_path = REPORTS_DIR / f"{run_id}.json"
    with open(flat_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    _print_summary(report)
    print(f"\nReport:  {report_path}")
    print(f"Renders: {run_dir / 'renders'}")

    if args.baseline:
        from benchmark.compare import compare_reports, print_comparison
        with open(args.baseline, encoding="utf-8") as f:
            base = json.load(f)
        print("\n" + "=" * 70)
        print(f"COMPARISON vs baseline {args.baseline}")
        print("=" * 70)
        diff = compare_reports(base, report)
        print_comparison(diff)
        if not diff["gate_passed"]:
            raise SystemExit(1)


def _print_summary(report: dict):
    s = report["summary"]["overall"]
    m = report["meta"]
    print("\n" + "=" * 70)
    print(f"EVAL SUMMARY  ({m['run_id']})")
    print("=" * 70)
    print(f"model={m['model']}  rag={'on' if m['rag_enabled'] else 'off'}  "
          f"n={m['n_repeats']}  cases={m['n_cases']}  wall={m.get('total_wall_s')}s")
    print(f"\n  pass@1            : {s['pass@1']['mean']:.1%}  (±{s['pass@1']['std']:.1%})")
    print(f"  pass@k (ceiling)  : {s['pass@k']:.1%}")
    print(f"  pass^k (stable)   : {s['pass^k']:.1%}")
    print(f"  verdict=pass      : {s['verdict_pass_rate']['mean']:.1%}")
    print(f"  printable         : {s['printable_rate']['mean']:.1%}")
    print(f"  one-shot          : {s['one_shot_rate']['mean']:.1%}")
    print(f"  avg wall          : {s['avg_wall_ms']}ms")
    print("\n  by difficulty:")
    for diff, d in report["summary"]["by_difficulty"].items():
        print(f"    {diff:9s}: pass@1 {d['pass@1']['mean']:.1%}  "
              f"verdict {d['verdict_pass_rate']['mean']:.1%}  (n={d['n_cases']})")
    print("\n  by path:")
    for p, d in report["summary"]["by_path"].items():
        print(f"    {p:12s}: pass@1 {d['pass@1']['mean']:.1%}  (n={d['n_cases']})")


if __name__ == "__main__":
    main()
