"""Build and persist developer-only executable CAD evaluation reports."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Sequence

from .contracts import (
    CaseVerdict,
    EvaluationCase,
    EvaluationReport,
    EvaluationRun,
    TestSeverity,
    TestStatus,
)


def _overall_verdict(cases: Sequence[EvaluationCase]) -> CaseVerdict:
    results = [case.result for case in cases]
    if CaseVerdict.ERROR in results:
        return CaseVerdict.ERROR
    if CaseVerdict.FAILED in results:
        return CaseVerdict.FAILED
    if CaseVerdict.BLOCKED in results:
        return CaseVerdict.BLOCKED
    if CaseVerdict.NOT_VERIFIED in results:
        return CaseVerdict.NOT_VERIFIED
    return CaseVerdict.VERIFIED


def _summary(cases: Sequence[EvaluationCase]) -> dict[str, Any]:
    results = [result for case in cases for result in case.tests]
    required = [result for result in results if result.severity is TestSeverity.REQUIRED]
    passed = [result for result in required if result.status is TestStatus.PASS]
    statuses = {status: sum(result.status is status for result in results) for status in TestStatus}
    return {
        "case_count": len(cases),
        "test_count": len(results),
        "required_total": len(required),
        "required_passed": len(passed),
        "required_pass_rate": len(passed) / len(required) if required else 0.0,
        "pass_count": statuses[TestStatus.PASS],
        "fail_count": statuses[TestStatus.FAIL],
        "unknown_count": statuses[TestStatus.UNKNOWN],
        "blocked_count": statuses[TestStatus.BLOCKED],
        "skipped_count": statuses[TestStatus.SKIPPED],
        "error_count": statuses[TestStatus.ERROR],
        "obligation_coverage": None,
    }


def build_report(run: EvaluationRun, cases: Sequence[EvaluationCase]) -> EvaluationReport:
    selected = tuple(cases)
    return EvaluationReport(
        run=run,
        summary=_summary(selected),
        verdict=_overall_verdict(selected),
        cases=selected,
    )


def write_report(report: EvaluationReport, path: Path) -> None:
    """Write a UTF-8 report atomically without reading environment secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
