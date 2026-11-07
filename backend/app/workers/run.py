"""Worker launcher with the same process-scoped evaluation mode as the API."""

from __future__ import annotations

import argparse
import os

from app.evaluation.mode import EvaluationMode, configure_process_mode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run CAD-Agent Temporal workers")
    parser.add_argument(
        "--evaluation-mode",
        choices=[mode.value for mode in EvaluationMode],
        default=EvaluationMode.DISABLED.value,
        help="developer evaluation mode for this Worker process",
    )
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--task-queue", default=None)
    parser.add_argument("--agent-v2-task-queue", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_process_mode(args.evaluation_mode)
    if args.namespace is not None:
        os.environ["TEMPORAL_NAMESPACE"] = args.namespace
    if args.task_queue is not None:
        os.environ["TEMPORAL_TASK_QUEUE"] = args.task_queue
    if args.agent_v2_task_queue is not None:
        os.environ["TEMPORAL_AGENT_V2_TASK_QUEUE"] = args.agent_v2_task_queue

    from app.workers.workflow_worker import run_worker
    import asyncio

    asyncio.run(run_worker())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
