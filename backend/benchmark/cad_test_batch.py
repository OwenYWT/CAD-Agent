"""CADTest batch evaluation owned by CAD-Agent.

The dataset is input data. All loading, scoring, native delivery checks, live
workflow submission, and report aggregation are implemented in this repository.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.evaluation.cad_test_observer import resolve_live_cli_config
from app.evaluation.cad_test_runner import (
    CadTestRunnerConfig,
    run_cad_test,
    run_cad_test_bundle,
)
from app.evaluation.mode import configure_process_mode
from benchmark import metrics
from benchmark.cad_test_engine.dataset import CadTestDataset, load_dataset
from benchmark.cad_test_report import write_html_report
from benchmark.live import LiveBenchmarkContext, run_live_case


def _default_dataset_root() -> Path | None:
    value = os.environ.get("CAD_TEST_DATASET_ROOT") or os.environ.get("CAD_TEST_ROOT")
    return Path(value) if value else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("dataset-check")
    check.add_argument("--dataset-root", type=Path, default=_default_dataset_root(), required=False)

    score = commands.add_parser("score")
    score.add_argument("--dataset-root", type=Path, default=_default_dataset_root(), required=False)
    score.add_argument("--case", required=True, dest="case_id")
    score.add_argument("--step", type=Path, required=True)
    score.add_argument("--fcstd", type=Path)
    score.add_argument("--stl", type=Path)
    score.add_argument("--out", type=Path, required=True)
    score.add_argument("--scorer-image", default=os.environ.get("CAD_TEST_SCORER_IMAGE", ""))
    score.add_argument("--timeout-seconds", type=int, default=180)

    live = commands.add_parser("live", help="run dataset prompts through the durable CAD-Agent path")
    live.add_argument("--dataset-root", type=Path, default=_default_dataset_root(), required=False)
    live.add_argument("--cases", default="all", help="all or comma-separated case IDs")
    live.add_argument("--limit", type=int)
    live.add_argument("--repeat", type=int, default=1)
    live.add_argument("--document-id", type=UUID)
    live.add_argument("--project-id", type=UUID)
    live.add_argument("--branch-id", type=UUID)
    live.add_argument("--base-revision-id", type=UUID)
    live.add_argument("--timeout-seconds", type=float, default=180.0)
    live.add_argument("--evaluation-timeout-seconds", type=float, default=420.0)
    live.add_argument("--report-root", type=Path)

    return parser


def _require_dataset_root(value: Path | None) -> Path:
    if value is None:
        raise SystemExit(
            "CADTest dataset root is required; pass --dataset-root or set CAD_TEST_DATASET_ROOT"
        )
    return value


def _select_cases(dataset: CadTestDataset, selection: str, limit: int | None) -> list[dict[str, Any]]:
    if selection == "all":
        selected = [dataset.case(str(case["id"])) for case in dataset.cases]
    else:
        requested = [item.strip() for item in selection.split(",") if item.strip()]
        unknown = sorted(set(requested).difference(dataset.case_ids))
        if unknown:
            raise SystemExit(f"unknown CADTest case ID(s): {unknown}")
        selected = [dataset.case(case_id) for case_id in requested]
    if limit is not None:
        if limit < 1:
            raise SystemExit("--limit must be positive")
        selected = selected[:limit]
    return [
        {
            **case,
            "id": str(case["id"]),
            "description": str(case.get("prompt") or ""),
            "path": str(case.get("path") or "extrude_cut"),
            "cad_test_required": True,
            "difficulty": str(case.get("difficulty") or "unknown"),
        }
        for case in selected
    ]


def _write_batch_report(output: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    report_path = output / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    html_path = write_html_report(report_path)
    return report_path, html_path


def _score(args: argparse.Namespace, dataset: CadTestDataset) -> int:
    configure_process_mode("observe")
    case = dataset.case(args.case_id)
    config = CadTestRunnerConfig(
        root=args.out,
        dataset_root=dataset.root,
        scorer_image=args.scorer_image,
        timeout_seconds=args.timeout_seconds,
    )
    artifacts = {"step": args.step}
    if args.fcstd is not None:
        artifacts["fcstd"] = args.fcstd
    if args.stl is not None:
        artifacts["stl"] = args.stl
    if args.fcstd is not None or args.stl is not None:
        result = run_cad_test_bundle(
            artifacts=artifacts,
            case_id=str(case["id"]),
            workflow_id="batch-score",
            candidate_revision_id="batch-score",
            config=config,
        )
    else:
        result = run_cad_test(
            artifact_path=args.step,
            case_id=str(case["id"]),
            workflow_id="batch-score",
            candidate_revision_id="batch-score",
            config=config,
        )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result and result.get("status") in {"pass", "passed"} else 1


async def _live(args: argparse.Namespace, dataset: CadTestDataset) -> int:
    if args.repeat < 1:
        raise SystemExit("--repeat must be positive")
    if args.evaluation_timeout_seconds <= 0:
        raise SystemExit("--evaluation-timeout-seconds must be positive")
    if args.document_id is not None and any(
        value is not None for value in (args.project_id, args.branch_id, args.base_revision_id)
    ):
        raise SystemExit("--document-id cannot be combined with legacy workspace IDs")
    if args.document_id is None and not all(
        value is not None for value in (args.project_id, args.branch_id, args.base_revision_id)
    ):
        raise SystemExit(
            "live batch requires --document-id or --project-id, --branch-id, --base-revision-id"
        )

    from app.domain.identity import local_anonymous_principal
    from benchmark.eval import _resolve_live_workspace
    from app.evaluation.cad_test_observer import (
        cad_test_required_from_environment,
        require_temporal_worker_build_id,
        validate_cad_test_worker_config,
    )

    if not cad_test_required_from_environment():
        raise SystemExit("Set CAD_TEST_REQUIRED=true in both the CLI and evaluator Worker environments")
    worker_build_id = require_temporal_worker_build_id()
    configured_dataset_hash = validate_cad_test_worker_config()
    if configured_dataset_hash != dataset.dataset_hash:
        raise SystemExit("CLI CADTest dataset hash differs from the validated Worker configuration")

    principal = local_anonymous_principal()
    project_id, branch_id, base_revision_id = await _resolve_live_workspace(
        document_id=args.document_id,
        project_id=args.project_id,
        branch_id=args.branch_id,
        base_revision_id=args.base_revision_id,
        principal=principal,
    )
    cases = _select_cases(dataset, args.cases, args.limit)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    output = args.report_root / "cad_test" / run_id
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "cad-agent-cadtest-batch.v1",
        "run_id": run_id,
        "mode": "live-product",
        "dataset_hash": dataset.dataset_hash,
        "expected_worker_build_id": worker_build_id,
        "case_ids": [case["id"] for case in cases],
        "repeat": args.repeat,
        "process_metrics_source": "live_evaluation_report",
        "cad_test_engine": "backend/benchmark/cad_test_engine",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    context = LiveBenchmarkContext(
        principal=principal,
        project_id=project_id,
        branch_id=branch_id,
        expected_base_revision_id=base_revision_id,
        report_root=args.report_root,
        timeout_seconds=args.timeout_seconds,
        cad_test_timeout_seconds=args.evaluation_timeout_seconds,
        run_id=run_id,
        dataset_hash=dataset.dataset_hash,
        worker_build_id=worker_build_id,
    )
    rows: list[dict[str, Any]] = []
    for repeat in range(1, args.repeat + 1):
        for case in cases:
            started = time.monotonic()
            try:
                row = await run_live_case(
                    case,
                    context=context,
                    repeat_index=repeat,
                )
            except Exception as exc:
                row = {
                    "executed": False,
                    "passed": False,
                    "case_id": case["id"],
                    "repeat": repeat,
                    "attempts": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                    "process_evaluation": {"status": "unavailable"},
                }
            row.update(
                {
                    "case_id": case["id"],
                    "repeat": repeat,
                    "wall_time_ms": int((time.monotonic() - started) * 1000),
                }
            )
            rows.append(row)
            with (output / "results.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    case_entries = []
    for case in cases:
        case_rows = [row for row in rows if row.get("case_id") == case["id"]]
        case_entries.append(
            {
                "id": case["id"],
                "difficulty": case["difficulty"],
                "path": case["path"],
                "description": case["description"],
                "runs": case_rows,
                "agg": metrics.aggregate_case(case_rows),
            }
        )
    report = {
        "schema_version": "cad-agent-cadtest-batch-report.v1",
        "manifest": manifest,
        "summary": metrics.aggregate_summary(case_entries),
        "cases": case_entries,
    }
    report_path, _html_path = _write_batch_report(output, report)
    print(report_path)
    return 0 if rows and all(row.get("passed") is True for row in rows) else 1


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "live":
        try:
            config, args.report_root = resolve_live_cli_config(
                dataset_root=args.dataset_root, report_root=args.report_root,
            )
        except ValueError as exc:
            parser.error(str(exc))
        args.dataset_root = config.dataset_root
    dataset = load_dataset(_require_dataset_root(getattr(args, "dataset_root", None)))
    if args.command == "dataset-check":
        print(
            json.dumps(
                {
                    "status": "validated",
                    "count": len(dataset.cases),
                    "dataset_hash": dataset.dataset_hash,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "score":
        return _score(args, dataset)
    configure_process_mode("observe")
    return asyncio.run(_live(args, dataset))


if __name__ == "__main__":
    raise SystemExit(main())
