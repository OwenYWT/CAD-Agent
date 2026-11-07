"""Versioned contracts for live developer evaluation sessions."""

from __future__ import annotations

from dataclasses import dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from .mode import EvaluationMode


class EvaluationStatus(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    ERROR = "error"


class LiveEventStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    ERROR = "error"
    SKIPPED = "skipped"


_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "token",
    "credential",
    "database_url",
    "access_key",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _contains_sensitive_key(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                return str(key)
            found = _contains_sensitive_key(item)
            if found:
                return found
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            found = _contains_sensitive_key(item)
            if found:
                return found
    return None


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in value.__dict__.items()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, set):
        return sorted(_json_value(item) for item in value)
    return value


def _validate_payload(value: Mapping[str, Any]) -> None:
    sensitive_key = _contains_sensitive_key(value)
    if sensitive_key:
        raise ValueError(f"sensitive evaluation payload field is not allowed: {sensitive_key}")


@dataclass(frozen=True)
class EvaluationEvidence:
    evidence_id: str
    source_kind: str
    source_ref: str | None = None
    authority: str = "unavailable"
    artifact_sha256: str | None = None
    observed: Mapping[str, Any] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("evidence_id must be non-empty")
        if not self.source_kind.strip():
            raise ValueError("source_kind must be non-empty")
        if self.artifact_sha256 is not None and (
            len(self.artifact_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.artifact_sha256)
        ):
            raise ValueError("artifact_sha256 must be a lower-case SHA-256 digest")
        _validate_payload(self.observed)
        _validate_payload(self.details)

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class EvaluationEvent:
    event_id: str
    event_type: str
    occurred_at: str
    sequence: int
    request_id: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    base_revision_id: str | None = None
    source_revision_id: str | None = None
    candidate_build_id: str | None = None
    candidate_revision_id: str | None = None
    change_set_id: str | None = None
    status: LiveEventStatus = LiveEventStatus.UNKNOWN
    evidence_ids: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.event_type.strip():
            raise ValueError("event_id and event_type must be non-empty")
        if self.sequence < 1:
            raise ValueError("event sequence must be positive")
        object.__setattr__(self, "status", LiveEventStatus(self.status))
        _validate_payload(self.payload)

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class EvaluationReport:
    evaluation_run_id: str
    mode: EvaluationMode
    source: Mapping[str, str]
    status: EvaluationStatus
    started_at: str
    finished_at: str | None
    timeline: tuple[EvaluationEvent, ...] = ()
    evidence: tuple[EvaluationEvidence, ...] = ()
    errors: tuple[str, ...] = ()
    request_id: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    base_revision_id: str | None = None
    source_revision_id: str | None = None
    candidate_build_id: str | None = None
    candidate_revision_id: str | None = None
    change_set_id: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    consistency: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = "cad-live-evaluation-report.v1"

    def __post_init__(self) -> None:
        if not self.evaluation_run_id.strip():
            raise ValueError("evaluation_run_id must be non-empty")
        object.__setattr__(self, "mode", EvaluationMode(self.mode))
        object.__setattr__(self, "status", EvaluationStatus(self.status))
        _validate_payload(self.source)
        _validate_payload(self.metrics)
        _validate_payload({"tool_calls": self.tool_calls})
        _validate_payload(self.consistency)
        if self.schema_version != "cad-live-evaluation-report.v1":
            raise ValueError("unsupported live evaluation report schema version")

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)
