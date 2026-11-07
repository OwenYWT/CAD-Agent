"""Atomic local persistence for developer-only live evaluation reports."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .human_report import render_human_report
from .session import EvaluationSession


def report_path(root: Path, evaluation_run_id: str) -> Path:
    return root / "live" / f"live_{evaluation_run_id}.json"


def human_report_path(root: Path, evaluation_run_id: str) -> Path:
    return report_path(root, evaluation_run_id).with_suffix(".md")


def markdown_path_for_json(target: Path) -> Path:
    return target.with_suffix(".md")


def _atomic_write(target: Path, encoded: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def write_report_payload(payload: Mapping[str, Any], target: Path) -> Path:
    _atomic_write(
        target,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(markdown_path_for_json(target), render_human_report(payload))
    return target


def write_session_report(session: EvaluationSession, target: Path) -> Path:
    return write_report_payload(session.to_report(), target)


def safe_write_session_report(session: EvaluationSession, target: Path) -> Path | None:
    """Persist a report without allowing evaluator errors into business code."""
    try:
        return write_session_report(session, target)
    except Exception as exc:
        session.record_error(f"evaluation report write failed: {exc}")
        return None
