"""Versioned, JSON-compatible contracts for developer-only CAD evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field, is_dataclass
from enum import StrEnum
from typing import Any, Mapping

from .mode import EvaluationMode


class TestStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    ERROR = "error"


class TestSeverity(StrEnum):
    REQUIRED = "required"
    DIAGNOSTIC = "diagnostic"


class CaseVerdict(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    NOT_VERIFIED = "not_verified"
    BLOCKED = "blocked"
    ERROR = "error"


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {
            key: _json_value(item)
            for key, item in value.__dict__.items()
        }
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, set):
        return sorted(_json_value(item) for item in value)
    return value


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class TestDefinition:
    test_id: str
    test_version: str
    kind: str
    severity: TestSeverity
    obligation_ids: tuple[str, ...] = ()
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text("test_id", self.test_id)
        _require_text("test_version", self.test_version)
        _require_text("kind", self.kind)
        object.__setattr__(self, "severity", TestSeverity(self.severity))
        if any(not isinstance(item, str) or not item.strip() for item in self.obligation_ids):
            raise ValueError("obligation_ids must contain non-empty strings")

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    source_kind: str
    source_ref: str | None = None
    authority: str = "unavailable"
    artifact_sha256: str | None = None
    observed: Mapping[str, Any] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text("evidence_id", self.evidence_id)
        _require_text("source_kind", self.source_kind)
        if self.artifact_sha256 is not None and (
            len(self.artifact_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.artifact_sha256)
        ):
            raise ValueError("artifact_sha256 must be a lower-case SHA-256 digest")

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class TestResult:
    test_id: str
    status: TestStatus
    severity: TestSeverity
    message: str
    evidence_ids: tuple[str, ...] = ()
    observed: Mapping[str, Any] | None = None
    expected: Mapping[str, Any] | None = None
    failure_code: str | None = None
    duration_ms: int = 0
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text("test_id", self.test_id)
        _require_text("message", self.message)
        object.__setattr__(self, "status", TestStatus(self.status))
        object.__setattr__(self, "severity", TestSeverity(self.severity))
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    input_text: str
    difficulty: str
    modeling_path: str
    tests: tuple[TestResult, ...] = ()
    result: CaseVerdict = CaseVerdict.NOT_VERIFIED
    artifacts: tuple[Evidence, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text("case_id", self.case_id)
        _require_text("difficulty", self.difficulty)
        _require_text("modeling_path", self.modeling_path)
        object.__setattr__(self, "result", CaseVerdict(self.result))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class EvaluationRun:
    run_id: str
    mode: EvaluationMode
    provenance: Mapping[str, Any] = field(default_factory=dict)
    budget: Mapping[str, Any] = field(default_factory=dict)
    cases: tuple[EvaluationCase, ...] = ()
    schema_version: str = "cad-evaluation-run.v1"

    def __post_init__(self) -> None:
        _require_text("run_id", self.run_id)
        object.__setattr__(self, "mode", EvaluationMode(self.mode))
        if self.schema_version != "cad-evaluation-run.v1":
            raise ValueError("unsupported evaluation run schema version")

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class EvaluationReport:
    run: EvaluationRun
    summary: Mapping[str, Any]
    verdict: CaseVerdict
    cases: tuple[EvaluationCase, ...] = ()
    schema_version: str = "cad-evaluation-report.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", CaseVerdict(self.verdict))
        if self.schema_version != "cad-evaluation-report.v1":
            raise ValueError("unsupported evaluation report schema version")

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)
