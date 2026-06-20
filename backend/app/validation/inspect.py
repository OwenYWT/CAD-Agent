"""Evidence inspect report — aggregate already-computed deterministic facts into one
structured report (forgecad-style manifest).

Pure aggregation: reads GeometryValidation (+ optional DFM dict) and produces an
InspectReport. NO geometry is loaded or recomputed here. Honesty contract: only checks
backed by real signals are emitted — nothing is fabricated.
"""

from app.models.schemas import (
    BoundingBox,
    InspectCheck,
    InspectReport,
    RuleViolationModel,
)
from app.validation.geometry_validator import GeometryValidation

# ValidationRule.severity → InspectCheck.status (only used when the rule did NOT pass;
# a passing rule is always "pass" regardless of its severity label).
_SEVERITY_TO_FAIL_STATUS = {"error": "fail", "warning": "warn", "info": "warn"}

_STATUS_RANK = {"pass": 0, "warn": 1, "fail": 2}


def _check_from_rule(rule) -> InspectCheck:
    """Map one GeometryValidation ValidationRule → InspectCheck."""
    if rule.passed:
        status = "pass"
    else:
        status = _SEVERITY_TO_FAIL_STATUS.get(rule.severity, "warn")
    return InspectCheck(
        name=rule.name, status=status, message=rule.message, source="geometry"
    )


def build_inspect_report(
    geo: GeometryValidation, dfm: dict | None = None
) -> InspectReport:
    """Build the evidence report from a GeometryValidation and an optional DFM dict.

    `dfm` is the dict produced by Orchestrator._run_auto_dfm (or None when DFM didn't
    run); only design_score and rule_violations are folded in.
    """
    checks = [_check_from_rule(r) for r in geo.rules]

    report = InspectReport(
        printable=geo.printable,
        is_watertight=geo.is_watertight,
        bounding_box=BoundingBox(**geo.bounding_box) if geo.bounding_box else None,
        volume=geo.volume,
        min_wall_thickness=geo.min_wall_thickness,
        checks=checks,
        print_warnings=list(geo.print_warnings),
    )

    # Optional DFM enrichment — reuse the already-computed dict, do not re-analyze.
    if dfm:
        report.design_score = dfm.get("design_score")
        report.dfm_violations = [
            RuleViolationModel(**rv) for rv in dfm.get("rule_violations", [])
        ]

    report.verdict = _worst_status(report.checks)
    return report


def _worst_status(checks: list[InspectCheck]) -> str:
    worst = "pass"
    for c in checks:
        if _STATUS_RANK.get(c.status, 0) > _STATUS_RANK[worst]:
            worst = c.status
    return worst


def add_check(report: InspectReport, check: InspectCheck) -> None:
    """Append a check (e.g. an indeterminate vision result) and recompute the verdict."""
    report.checks.append(check)
    report.verdict = _worst_status(report.checks)
