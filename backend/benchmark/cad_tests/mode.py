"""Evaluation mode parsing kept outside application startup and request paths."""

from __future__ import annotations

import os
from enum import StrEnum


class EvaluationMode(StrEnum):
    DISABLED = "disabled"
    OBSERVE = "observe"
    EXPERIMENTAL = "experimental"


def resolve_mode(value: str | None = None) -> EvaluationMode:
    """Resolve an explicit mode or the developer-only environment override.

    The missing environment variable intentionally resolves to ``disabled`` so
    importing the evaluator cannot create side effects in the application.
    """
    raw = value if value is not None else os.getenv("CAD_EVALUATION_MODE", "disabled")
    try:
        return EvaluationMode(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in EvaluationMode)
        raise ValueError(
            f"CAD_EVALUATION_MODE must be one of: {allowed}; got {raw!r}"
        ) from exc
