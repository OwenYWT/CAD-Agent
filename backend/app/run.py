"""Application launcher with an explicit developer evaluation mode."""

from __future__ import annotations

import argparse

from app.evaluation.mode import EvaluationMode, configure_process_mode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the CAD-Agent API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--evaluation-mode",
        choices=[mode.value for mode in EvaluationMode],
        default=EvaluationMode.DISABLED.value,
        help="developer evaluation mode for this API process",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_process_mode(args.evaluation_mode)

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
