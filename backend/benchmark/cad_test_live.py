"""Run the repository-owned CADTest scorer against a live artifact."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.evaluation.cad_test_runner import CadTestRunnerConfig
from app.evaluation.integration import attach_cad_test_artifacts, attach_cad_test_result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    artifact_group = parser.add_mutually_exclusive_group(required=True)
    artifact_group.add_argument("--artifact", "--step", type=Path, dest="step_artifact")
    parser.add_argument("--fcstd", type=Path)
    parser.add_argument("--stl", type=Path)
    parser.add_argument("--case", required=True, dest="case_id")
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--candidate-revision-id", required=True)
    parser.add_argument(
        "--report-root",
        type=Path,
        default=Path(__file__).resolve().parent / "reports",
    )
    parser.add_argument(
        "--cad-test-root",
        type=Path,
        default=Path(os.environ["CAD_TEST_ROOT"])
        if os.environ.get("CAD_TEST_ROOT")
        else None,
        required=not bool(os.environ.get("CAD_TEST_ROOT")),
    )
    parser.add_argument(
        "--scorer-image",
        default=os.environ.get("CAD_TEST_SCORER_IMAGE", ""),
    )
    parser.add_argument("--timeout-seconds", type=int, default=180)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "workflow_run_id": args.workflow_id,
        "candidate_revision_id": args.candidate_revision_id,
    }
    config = CadTestRunnerConfig(
        root=args.report_root / "cad_test",
        dataset_root=args.cad_test_root,
        scorer_image=args.scorer_image,
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
            root=args.report_root,
            config=config,
        )
    else:
        result = attach_cad_test_result(
            payload,
            args.step_artifact,
            case_id=args.case_id,
            root=args.report_root,
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
    report_path = args.report_root / "live" / f"live_live-{args.workflow_id}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "evaluation_run_id": result,
                "report_path": str(report_path),
                "cad_test": report.get("cad_test"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
