"""DFM rule evaluation engine — pure deterministic geometric + heuristic checks.

No LLM. Geometric rules compare measured geometry against thresholds. "Heuristic"
rules that require true B-rep reasoning we don't compute (undercut, tool access,
parting line) are surfaced as advisory INFO notes for the user to confirm manually,
rather than fabricating an unreliable pass/fail (which is what the old LLM path did).
"""

import logging
from typing import TYPE_CHECKING


from app.dfm.models import DFMRule, RuleViolation
from app.dfm import rule_store
from app.validation.design_analyzer import DFMGeometryResult

if TYPE_CHECKING:
    from app.dfm.models import StepAnalysisResult

logger = logging.getLogger(__name__)


# Map process category to knowledge graph process IDs
_PROCESS_KG_MAP = {
    "CNC": ["proc_cnc_3axis", "proc_cnc_5axis"],
    "FDM": ["proc_fdm"],
    "SLA": ["proc_sla"],
    "injection_mold": ["proc_injection"],
    "sheet_metal": ["proc_sheet"],
}

# Map rule categories to knowledge graph constraint keys
_CATEGORY_KG_KEY = {
    "wall_thickness": "min_wall",
    "size": "max_size",
    "hole": "min_hole_diameter",
    "draft_angle": "min_draft_angle",
    "fillet": "min_internal_radius",
}


class DFMRuleEngine:
    def __init__(self):
        pass

    async def _enrich_from_knowledge_graph(
        self,
        rules: list[DFMRule],
        material: str | None = None,
    ) -> list[DFMRule]:
        """Optionally override rule thresholds from knowledge graph constraints."""
        try:
            from app.dfm.knowledge_graph import get_process_constraints
        except ImportError:
            return rules

        enriched = []
        for rule in rules:
            kg_ids = _PROCESS_KG_MAP.get(rule.process, [])
            kg_key = _CATEGORY_KG_KEY.get(rule.category)

            if not kg_ids or not kg_key:
                enriched.append(rule)
                continue

            # Query KG for the first matching process
            for pid in kg_ids:
                try:
                    constraints = await get_process_constraints(pid, material_id=material)
                except Exception:
                    continue

                val = constraints.get(kg_key)
                if val is not None:
                    # Override threshold from KG
                    rule_copy = rule.model_copy()
                    if kg_key.startswith("min"):
                        rule_copy.threshold_min = float(val)
                    elif kg_key.startswith("max"):
                        rule_copy.threshold_max = float(val)
                    enriched.append(rule_copy)
                    break
            else:
                enriched.append(rule)

        return enriched

    async def evaluate(
        self,
        geometry: DFMGeometryResult,
        process: str | None = None,
        code: str = "",
        step_data: "StepAnalysisResult | None" = None,
        material: str | None = None,
    ) -> list[RuleViolation]:
        """Evaluate all enabled rules against geometry data."""
        if process:
            rules = await rule_store.get_rules_by_process(process)
        else:
            rules = await rule_store.get_all_enabled_rules()

        if not rules:
            return []

        # Enrich thresholds from knowledge graph if material is specified
        if material:
            rules = await self._enrich_from_knowledge_graph(rules, material)

        violations: list[RuleViolation] = []

        # Split into geometric and heuristic
        geometric_rules = [r for r in rules if r.check_type == "geometric" and r.enabled]
        heuristic_rules = [r for r in rules if r.check_type == "heuristic" and r.enabled]

        # Evaluate geometric rules (deterministic)
        for rule in geometric_rules:
            v = self._eval_geometric(rule, geometry, step_data)
            if v:
                violations.append(v)

        # Evaluate heuristic rules deterministically (geometric signal where available,
        # advisory info notes otherwise). No LLM.
        for rule in heuristic_rules:
            v = self._eval_heuristic(rule, geometry, step_data)
            if v:
                violations.append(v)

        # Sort by severity
        severity_order = {"critical": 0, "warning": 1, "info": 2}
        violations.sort(key=lambda v: severity_order.get(v.severity, 9))

        return violations

    def _eval_geometric(
        self,
        rule: DFMRule,
        geo: DFMGeometryResult,
        step_data: "StepAnalysisResult | None" = None,
    ) -> RuleViolation | None:
        """Evaluate a single geometric rule against geometry data."""
        actual = self._get_actual_value(rule, geo, step_data)
        if actual is None:
            return None

        violated = False
        if rule.threshold_min is not None and actual < rule.threshold_min:
            violated = True
        if rule.threshold_max is not None and actual > rule.threshold_max:
            violated = True

        if not violated:
            return None

        # Format message from template
        msg = rule.suggestion_template.format(
            actual=actual,
            min=rule.threshold_min or 0,
            max=rule.threshold_max or 0,
            min_x2=(rule.threshold_min or 0) * 2,
        ) if rule.suggestion_template else rule.description

        return RuleViolation(
            rule_id=rule.id,
            rule=rule,
            actual_value=actual,
            message=f"{rule.description}: {rule.category} = {actual:.2f} {rule.unit}",
            suggestion=msg,
            severity=rule.severity,
            source="geometric",
        )

    def _get_actual_value(
        self,
        rule: DFMRule,
        geo: DFMGeometryResult,
        step_data: "StepAnalysisResult | None" = None,
    ) -> float | None:
        """Extract the relevant value from geometry data for a rule.

        Prefers STEP-derived metrics when available (precise B-rep analysis),
        falls back to trimesh approximations.
        """
        cat = rule.category
        dm = step_data.derived_metrics if step_data and not step_data.error else None

        if cat == "wall_thickness":
            # STEP precision > trimesh approximation
            if dm and dm.min_wall_thickness is not None:
                return dm.min_wall_thickness
            return geo.min_wall_thickness if geo.min_wall_thickness > 0 else None

        if cat == "overhang":
            return geo.overhang_ratio

        if cat == "size":
            if geo.bounding_box:
                extents = [
                    geo.bounding_box.get("x_max", 0) - geo.bounding_box.get("x_min", 0),
                    geo.bounding_box.get("y_max", 0) - geo.bounding_box.get("y_min", 0),
                    geo.bounding_box.get("z_max", 0) - geo.bounding_box.get("z_min", 0),
                ]
                return max(extents)
            return None

        if cat == "fillet":
            # Precise fillet data from STEP analysis
            if dm and dm.min_fillet_radius is not None:
                return dm.min_fillet_radius
            return None

        if cat == "hole":
            # Precise hole diameter from STEP analysis
            if dm and dm.min_hole_diameter is not None:
                return dm.min_hole_diameter
            return None

        if cat == "draft_angle":
            # Precise draft angle from STEP analysis
            if dm and dm.min_draft_angle is not None:
                return dm.min_draft_angle
            return None

        return None

    def _eval_heuristic(
        self,
        rule: DFMRule,
        geo: DFMGeometryResult,
        step_data: "StepAnalysisResult | None" = None,
    ) -> RuleViolation | None:
        """Deterministic evaluation of a 'heuristic' rule.

        A few have genuine geometric signal (closed-cavity detection). The rest
        require true B-rep undercut / accessibility / parting-line analysis we don't
        compute yet — for those we emit an advisory INFO note (the rule's reminder),
        NOT a fabricated pass/fail. The advisory is downgraded to severity='info'
        because we cannot confirm an actual violation.
        """
        # Closed-cavity detection for SLA drain holes: a watertight mesh with very
        # high solidity is unlikely to have internal cavities → no drain issue.
        # A watertight mesh with a hollow interior (low-ish material ratio but no
        # opening) is the risky case. We surface SLA drain reminders only when hollow.
        if rule.id == "sla_drain_hole":
            if geo.is_watertight and geo.material_ratio and geo.material_ratio < 0.6:
                return RuleViolation(
                    rule_id=rule.id, rule=rule,
                    message=f"{rule.description}: 检测到可能的封闭空腔 (实心率 {geo.material_ratio:.0%})",
                    suggestion=rule.suggestion_template,
                    severity="warning",
                    source="heuristic",
                )
            return None

        # Everything else: advisory reminder (info), since we can't deterministically
        # confirm undercut / tool access / parting line without full B-rep reasoning.
        return RuleViolation(
            rule_id=rule.id, rule=rule,
            message=f"[需人工确认] {rule.description}",
            suggestion=rule.suggestion_template,
            severity="info",
            source="heuristic",
        )
