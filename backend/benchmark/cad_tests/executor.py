"""Execute registered CADTests without invoking production generation."""

from __future__ import annotations

from collections.abc import Sequence

from .contracts import CaseVerdict, EvaluationCase, TestDefinition, TestResult, TestSeverity, TestStatus
from .definitions import HANDLERS, v0_definitions
from .evidence import EvidenceContext


def _case_verdict(results: Sequence[TestResult]) -> CaseVerdict:
    required = [item for item in results if item.severity is TestSeverity.REQUIRED]
    if any(item.status is TestStatus.ERROR for item in required):
        return CaseVerdict.ERROR
    if any(item.status is TestStatus.FAIL for item in required):
        return CaseVerdict.FAILED
    if any(item.status is TestStatus.BLOCKED for item in required):
        return CaseVerdict.BLOCKED
    if any(item.status in {TestStatus.UNKNOWN, TestStatus.SKIPPED} for item in required):
        return CaseVerdict.NOT_VERIFIED
    return CaseVerdict.VERIFIED


def _unsupported_result(definition: TestDefinition, message: str, *, error: bool) -> TestResult:
    return TestResult(
        test_id=definition.test_id,
        status=TestStatus.ERROR if error else TestStatus.BLOCKED,
        severity=definition.severity,
        message=message,
        failure_code="unsupported_test_kind",
    )


def execute_case(
    case: dict,
    context: EvidenceContext,
    *,
    definitions: Sequence[TestDefinition] | None = None,
) -> EvaluationCase:
    selected = tuple(definitions) if definitions is not None else v0_definitions(case)
    results: list[TestResult] = []
    for definition in selected:
        handler = HANDLERS.get(definition.kind)
        if handler is None:
            results.append(_unsupported_result(definition, "test kind is not registered", error=True))
            continue
        try:
            results.append(handler(definition, context))
        except Exception as exc:
            results.append(
                TestResult(
                    test_id=definition.test_id,
                    status=TestStatus.ERROR,
                    severity=definition.severity,
                    message=f"test handler raised {type(exc).__name__}",
                    failure_code="test_handler_error",
                    details={"error_type": type(exc).__name__},
                )
            )

    verdict = _case_verdict(results)
    return EvaluationCase(
        case_id=str(case.get("id") or "unknown"),
        input_text=str(case.get("description") or ""),
        difficulty=str(case.get("difficulty") or "unknown"),
        modeling_path=str(case.get("path") or "unknown"),
        tests=tuple(results),
        result=verdict,
        artifacts=context.evidence,
        metrics={"legacy_passed": context.row.get("passed")},
    )
