"""Offline, developer-only executable CAD evaluation over an existing report."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmark.cad_tests.evidence import normalize_benchmark_row
from benchmark.cad_tests.executor import execute_case
from benchmark.cad_tests.mode import EvaluationMode, resolve_mode
from benchmark.cad_tests.report import build_report, write_report
from benchmark.cad_tests.contracts import EvaluationRun


_SAFE_PROVENANCE_KEYS = (
    "git_sha",
    "case_set_hash",
    "model",
    "llm_provider",
    "temperature",
    "rag_enabled",
    "runtime_identity",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Developer-only offline CADTest evaluator")
    parser.add_argument("--input", type=Path, help="existing benchmark report JSON")
    parser.add_argument("--output", type=Path, help="developer evaluation report JSON")
    parser.add_argument("--mode", choices=("disabled", "observe"), default=None)
    parser.add_argument("--case", default="all", help="all or comma-separated source case IDs")
    return parser


def _provenance(meta: dict[str, Any]) -> dict[str, Any]:
    return {key: meta[key] for key in _SAFE_PROVENANCE_KEYS if key in meta}


def _source_cases(payload: dict[str, Any], selection: str) -> list[dict[str, Any]]:
    requested = None if selection == "all" else {item.strip() for item in selection.split(",") if item.strip()}
    cases = []
    for case in payload.get("cases") or ():
        if requested is not None and case.get("id") not in requested:
            continue
        cases.append(case)
    unknown = requested.difference({case.get("id") for case in cases}) if requested is not None else set()
    if unknown:
        raise ValueError(f"unknown case id(s): {sorted(unknown)}")
    return cases


def _evaluate_payload(payload: dict[str, Any], selection: str) -> tuple[EvaluationRun, tuple]:
    source_cases = _source_cases(payload, selection)
    evaluated = []
    for source_case in source_cases:
        runs = source_case.get("runs") or [source_case]
        for index, row in enumerate(runs, start=1):
            case = dict(source_case)
            if len(runs) > 1:
                case["id"] = f"{source_case.get('id', 'case')}/run-{index}"
            context = normalize_benchmark_row(case, row)
            evaluated.append(execute_case(case, context))

    meta = payload.get("meta") or {}
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = EvaluationRun(
        run_id=f"cad_eval_{timestamp}",
        mode=EvaluationMode.OBSERVE,
        provenance={
            **_provenance(meta),
            "evaluator_version": "cad-evaluator.v0",
            "test_suite_version": "cad-tests.v1",
        },
        budget={"candidate_limit": 1, "repair_limit": 0, "time_limit_seconds": 0},
        cases=tuple(evaluated),
    )
    return run, tuple(evaluated)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mode = resolve_mode(args.mode)
    if mode is EvaluationMode.DISABLED:
        print("CAD evaluation disabled; no report written.")
        return 0
    if mode is not EvaluationMode.OBSERVE:
        raise SystemExit("experimental evaluation is not available in v0")
    if args.input is None or args.output is None:
        raise SystemExit("observe mode requires --input and --output")

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    run, cases = _evaluate_payload(payload, args.case)
    report = build_report(run, cases)
    write_report(report, args.output)
    print(f"CAD evaluation report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
