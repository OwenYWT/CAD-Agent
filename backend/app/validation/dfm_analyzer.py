"""Deterministic design-review / DFM analysis (rule engine + geometry, NO LLM)."""

import asyncio
import logging
from app.dfm.models import rule_process
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.dfm.models import StepAnalysisResult


from app.validation.design_analyzer import DesignGeometryAnalyzer, DFMGeometryResult

logger = logging.getLogger(__name__)


@dataclass
class DFMIssue:
    category: str
    severity: str
    description: str
    suggestion: str
    location: str


@dataclass
class DesignAnalysis:
    design_score: int | None = None
    evaluation_status: str = "indeterminate"
    evaluated_rules: list[dict] = field(default_factory=list)
    analysis_errors: list[str] = field(default_factory=list)
    design_summary: str = ""
    structural_issues: list[str] = field(default_factory=list)
    functional_notes: list[str] = field(default_factory=list)
    recommended_process: str = ""
    process_compatibility: dict[str, str] = field(default_factory=dict)
    dfm_issues: list[DFMIssue] = field(default_factory=list)
    estimated_difficulty: str = "medium"
    geometry: DFMGeometryResult | None = None
    rule_violations: list[dict] = field(default_factory=list)  # from rule engine
    step_data: object = None  # StepAnalysisResult when available


class DFMAnalyzer:
    """Deterministic geometry + rule-based design review and DFM analysis (no LLM)."""

    def __init__(self):
        self._geo_analyzer = DesignGeometryAnalyzer()
        self._rule_engine = None
        self._step_analyzer = None

    @property
    def rule_engine(self):
        if self._rule_engine is None:
            from app.dfm.rule_engine import DFMRuleEngine
            self._rule_engine = DFMRuleEngine()
        return self._rule_engine

    @property
    def step_analyzer(self):
        if self._step_analyzer is None:
            from app.dfm.step_analysis import StepAnalyzer
            self._step_analyzer = StepAnalyzer()
        return self._step_analyzer

    async def analyze(
        self,
        stl_path: Path,
        code: str = "",
        description: str = "",
        process: str | None = None,
        step_path: Path | None = None,
        material: str | None = None,
        precomputed_step_data: object | None = None,
        rule_configuration: dict | None = None,
    ) -> DesignAnalysis:
        process = rule_process(process)
        # Step 1: Geometry analysis
        # Use STEP-based precise analysis if available, fall back to trimesh
        step_result = precomputed_step_data
        if step_result is None and step_path and step_path.exists():
            try:
                from app.dfm.models import StepAnalysisResult
                step_result = await self.step_analyzer.analyze(step_path)
                if step_result.error:
                    logger.warning(f"STEP analysis failed: {step_result.error}, falling back to trimesh")
                    step_result = None
                else:
                    logger.info(
                        f"STEP analysis OK: {len(step_result.faces)} faces, "
                        f"{len(step_result.features)} features"
                    )
            except Exception as e:
                logger.warning(f"STEP analysis error: {e}, falling back to trimesh")

        # Always run trimesh analysis (geometry metrics for the rule engine + fallback)
        geo_result = await asyncio.to_thread(
            self._geo_analyzer.analyze,
            stl_path,
        )

        # Enrich geo_result with STEP-derived precision data
        if step_result:
            geo_result = self._merge_step_data(geo_result, step_result)

        # Step 2: Rule engine evaluation (deterministic + heuristic)
        evaluation_errors = []
        evaluations = []
        try:
            violations, evaluations = await self.rule_engine.evaluate_with_evidence(
                geo_result, process=process, code=code,
                step_data=step_result, material=material,
                rule_configuration=rule_configuration,
            )
        except Exception as e:
            logger.warning(f"Rule engine evaluation failed: {e}")
            violations = []
            evaluation_errors.append(f"Rule engine evaluation failed: {type(e).__name__}")

        # Step 3: Deterministic scoring derived from rule violations (no LLM/VLM).
        from app.dfm import dfm_scoring

        incomplete = bool(evaluation_errors) or not evaluations or any(item["status"] == "indeterminate" for item in evaluations)
        if not any(item["status"] in {"passed", "violated"} for item in evaluations):
            incomplete = True
        score = None if incomplete else dfm_scoring.compute_design_score(violations)
        compat = dfm_scoring.compute_process_compatibility(violations)
        for evaluated_process in {item['process'] for item in evaluations} | ({process} if process else set()):
            rows = [item for item in evaluations if item['process'] == evaluated_process]
            if compat.get(evaluated_process) == '不适合':
                continue
            if not any(item['status'] in {'passed', 'violated'} for item in rows) or any(item['status'] == 'indeterminate' for item in rows):
                compat[evaluated_process] = '未验证'
            elif evaluated_process not in compat:
                compat[evaluated_process] = '需人工确认' if any(item['status'] == 'advisory' for item in rows) else '适合'
        summary = "检查证据不完整，无法确认可制造性。" if incomplete else dfm_scoring.build_summary(score, violations)
        evaluation_status = "failed" if any(v.severity == "critical" for v in violations) else "indeterminate" if incomplete else "warning" if any(v.severity == "warning" for v in violations) else "passed"
        recommended = process or next((name for name, outcome in compat.items() if outcome == '适合'), '')

        n_crit = sum(1 for v in violations if v.severity == "critical")
        n_warn = sum(1 for v in violations if v.severity == "warning")
        difficulty = "hard" if n_crit else ("medium" if n_warn else "easy")

        dfm_issues = [
            DFMIssue(
                category=v.rule.category,
                severity=v.severity,
                description=v.message,
                suggestion=v.suggestion,
                location=f"[{v.source}] {v.rule.process}",
            )
            for v in violations
        ]

        return DesignAnalysis(
            design_score=score,
            evaluation_status=evaluation_status,
            evaluated_rules=evaluations,
            analysis_errors=evaluation_errors,
            design_summary=summary,
            structural_issues=[],   # structural/functional review was subjective VLM output — removed
            functional_notes=[],
            recommended_process=recommended,
            process_compatibility=compat,
            dfm_issues=dfm_issues,
            estimated_difficulty=difficulty,
            geometry=geo_result,
            step_data=step_result,
            rule_violations=[
                {
                    "rule_id": v.rule_id,
                    "process": v.rule.process,
                    "category": v.rule.category,
                    "severity": v.severity,
                    "source": v.source,
                    "actual_value": v.actual_value,
                    "message": v.message,
                    "suggestion": v.suggestion,
                }
                for v in violations
            ],
        )

    @staticmethod
    def _merge_step_data(geo: DFMGeometryResult, step: "StepAnalysisResult") -> DFMGeometryResult:
        """Overwrite trimesh approximations with precise STEP data where available."""
        dm = step.derived_metrics
        gp = step.global_properties

        if dm.min_wall_thickness is not None and dm.min_wall_thickness > 0:
            geo.min_wall_thickness = dm.min_wall_thickness
            geo.has_thin_walls = dm.min_wall_thickness < 1.0

        if gp.volume > 0:
            geo.volume = round(gp.volume, 2)
        if gp.surface_area > 0:
            geo.surface_area = round(gp.surface_area, 2)
        if gp.face_count > 0:
            geo.face_count = gp.face_count
        if gp.bounding_box:
            geo.bounding_box = gp.bounding_box
            # Recompute material ratio with precise values
            bb = gp.bounding_box
            extents = [
                bb.get("x_max", 0) - bb.get("x_min", 0),
                bb.get("y_max", 0) - bb.get("y_min", 0),
                bb.get("z_max", 0) - bb.get("z_min", 0),
            ]
            bbox_vol = extents[0] * extents[1] * extents[2]
            if bbox_vol > 0 and gp.volume > 0:
                geo.material_ratio = round(gp.volume / bbox_vol, 3)

        return geo
