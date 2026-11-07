"""Process-scoped evaluation mode resolution."""

from __future__ import annotations

import os
from enum import StrEnum


class EvaluationMode(StrEnum):
    DISABLED = "disabled"
    OBSERVE = "observe"
    EXPERIMENTAL = "experimental"


def resolve_mode(value: str | None = None) -> EvaluationMode:
    """Resolve an explicit value or the process environment.

    The default is deliberately disabled. Request payloads are not consulted,
    so a client cannot enable developer evaluation for an individual request.
    """
    raw = value if value is not None else os.getenv("CAD_EVALUATION_MODE", "disabled")
    try:
        return EvaluationMode(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in EvaluationMode)
        raise ValueError(
            f"CAD_EVALUATION_MODE must be one of: {allowed}; got {raw!r}"
        ) from exc


def configure_process_mode(value: str) -> EvaluationMode:
    """Set and validate the mode before importing application components."""
    mode = resolve_mode(value)
    os.environ["CAD_EVALUATION_MODE"] = mode.value
    return mode
