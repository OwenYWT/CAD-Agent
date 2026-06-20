"""Deterministic DFM scoring — derives design_score, process_compatibility, and a
summary from rule-engine violations. Replaces the old VLM-generated subjective fields.

Everything here is a pure function of the violations + geometry, so the same part
always yields the same score (testable, explainable, no LLM)."""
from app.dfm.models import RuleViolation

# Point deductions per violation severity.
_SEVERITY_WEIGHT = {"critical": 20, "warning": 5, "info": 0}

# Processes we surface compatibility for.
_PROCESSES = ["CNC", "FDM", "SLA", "injection_mold", "sheet_metal", "die_casting"]


def compute_design_score(violations: list[RuleViolation]) -> int:
    """100 minus weighted deductions, floored at 0. Info-level notes don't deduct."""
    score = 100
    for v in violations:
        score -= _SEVERITY_WEIGHT.get(v.severity, 0)
    return max(0, min(100, score))


def compute_process_compatibility(violations: list[RuleViolation]) -> dict[str, str]:
    """Per process: '不适合' if any critical violation, '需修改' if any warning,
    else '适合'. Only processes that actually have violations or were evaluated appear."""
    by_process: dict[str, list[RuleViolation]] = {}
    for v in violations:
        by_process.setdefault(v.rule.process, []).append(v)

    compat: dict[str, str] = {}
    for proc, vs in by_process.items():
        if any(v.severity == "critical" for v in vs):
            compat[proc] = "不适合"
        elif any(v.severity == "warning" for v in vs):
            compat[proc] = "需修改"
        else:
            compat[proc] = "适合"
    return compat


def recommend_process(compat: dict[str, str], default: str | None = None) -> str:
    """Pick the most compatible process (适合 > 需修改 > 不适合)."""
    rank = {"适合": 0, "需修改": 1, "不适合": 2}
    if not compat:
        return default or ""
    best = min(compat.items(), key=lambda kv: rank.get(kv[1], 9))
    return best[0]


def build_summary(score: int, violations: list[RuleViolation]) -> str:
    """One-line deterministic summary."""
    n_crit = sum(1 for v in violations if v.severity == "critical")
    n_warn = sum(1 for v in violations if v.severity == "warning")
    if n_crit:
        return f"可制造性评分 {score}/100：发现 {n_crit} 个严重问题、{n_warn} 个警告，需修改后再制造。"
    if n_warn:
        return f"可制造性评分 {score}/100：发现 {n_warn} 个警告项，建议优化。"
    return f"可制造性评分 {score}/100：未发现明显可制造性问题。"
