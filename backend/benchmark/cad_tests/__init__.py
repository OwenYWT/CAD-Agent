"""Developer-only executable CAD evaluation primitives."""

from .contracts import (
    CaseVerdict,
    EvaluationCase,
    EvaluationReport,
    EvaluationRun,
    Evidence,
    TestDefinition,
    TestResult,
    TestSeverity,
    TestStatus,
)
from .mode import EvaluationMode, resolve_mode

__all__ = [
    "CaseVerdict",
    "EvaluationCase",
    "EvaluationMode",
    "EvaluationReport",
    "EvaluationRun",
    "Evidence",
    "TestDefinition",
    "TestResult",
    "TestSeverity",
    "TestStatus",
    "resolve_mode",
]
