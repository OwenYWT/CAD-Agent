"""Run the repository-owned CADTest scorer against a live artifact."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from app.evaluation.cad_test_observer import resolve_live_cli_config
from app.evaluation.integration import attach_cad_test_artifacts, attach_cad_test_result
from app.evaluation.store import FileEvaluationStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    artifact_group = parser.add_mutually_exclusive_group(required=True)
    artifact_group.add_argument("--artifact", "--step", type=Path, dest="step_artifact")
    parser.add_argument("--fcstd", type=Path)
    parser.add_argument("--stl", type=Path)
    parser.add_argument("--case", required=True, dest="case_id")
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--candidate-revision-id", required=True)
    parser.add_argument("--report-root", type=Path)
    parser.add_argument("--dataset-root", "--cad-test-root", type=Path)
    parser.add_argument("--scorer-image")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config, report_root = resolve_live_cli_config(
            dataset_root=args.dataset_root, report_root=args.report_root,
        )
    except ValueError as exc:
        parser.error(str(exc))
    payload = {
        "workflow_run_id": args.workflow_id,
        "candidate_revision_id": args.candidate_revision_id,
    }
    config = replace(
        config,
        scorer_image=args.scorer_image or config.scorer_image,
        timeout_seconds=args.timeout_seconds,
    )
    if args.fcstd is not None or args.stl is not None:
        artifacts = {"step": args.step_artifact}
        if args.fcstd is not None:
            artifacts["fcstd"] = args.fcstd
        if args.stl is not None:
            artifacts["stl"] = args.stl
        result = attach_cad_test_artifacts(
            payload,
            artifacts,
            case_id=args.case_id,
            root=report_root,
            config=config,
        )
    else:
        result = attach_cad_test_result(
            payload,
            args.step_artifact,
            case_id=args.case_id,
            root=report_root,
            config=config,
        )
    if result is None:
        print(
            json.dumps(
                {
                    "status": "disabled",
                    "reason": "CAD_EVALUATION_MODE must be observe",
                },
                ensure_ascii=False,
            )
        )
        return 2
    report_path = FileEvaluationStore(report_root).path_for(result)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = None
    observation = report.get("cad_test") if isinstance(report, dict) else None
    if not isinstance(observation, dict):
        observation = {
            "status": "unavailable",
            "reason": "cad_test_result_missing" if isinstance(report, dict) else "live_report_unavailable",
        }
    print(
        json.dumps(
            {
                "evaluation_run_id": result,
                "report_path": str(report_path),
                "cad_test": observation,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if observation.get("status") == "passed" and observation.get("verdict") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
