"""DFM is now LLM-free: rule engine + scoring + analyzer all run deterministically
with NO dashscope key. Hermetic (no Docker, no LLM)."""
import tempfile
from pathlib import Path

import pytest
import trimesh

from app.config import settings
from app.dfm import dfm_scoring
from app.dfm.models import DFMRule, RuleViolation
from app.dfm.rule_engine import DFMRuleEngine
from app.validation.design_analyzer import DFMGeometryResult


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    """Hard guarantee: DFM must work with no LLM credentials at all."""
    monkeypatch.setattr(settings, "dashscope_api_key", None)


def _rule(**kw):
    base = dict(id="r", process="FDM", category="wall_thickness",
                check_type="geometric", severity="critical", suggestion_template="t")
    base.update(kw)
    return DFMRule(**base)


# === scoring (pure functions) ===

def test_score_deducts_by_severity():
    rs = [_rule(id="a", severity="critical"), _rule(id="b", severity="warning")]
    vs = [RuleViolation(rule_id=r.id, rule=r, severity=r.severity, source="geometric") for r in rs]
    assert dfm_scoring.compute_design_score(vs) == 100 - 20 - 5  # 75


def test_score_floors_at_zero():
    rs = [_rule(id=f"c{i}", severity="critical") for i in range(10)]
    vs = [RuleViolation(rule_id=r.id, rule=r, severity="critical", source="geometric") for r in rs]
    assert dfm_scoring.compute_design_score(vs) == 0


def test_process_compatibility_levels():
    crit = _rule(id="a", process="injection_mold", severity="critical")
    warn = _rule(id="b", process="CNC", severity="warning")
    info = _rule(id="c", process="FDM", severity="info")
    vs = [RuleViolation(rule_id=r.id, rule=r, severity=r.severity, source="geometric") for r in (crit, warn, info)]
    compat = dfm_scoring.compute_process_compatibility(vs)
    assert compat["injection_mold"] == "不适合"
    assert compat["CNC"] == "需修改"
    assert compat["FDM"] == "适合"
    # recommend picks the most compatible
    assert dfm_scoring.recommend_process(compat) == "FDM"


# === rule engine (deterministic, no LLM) ===

@pytest.mark.asyncio
async def test_rule_engine_geometric_fires_without_key():
    """A thin-walled FDM part trips the geometric wall-thickness rule — no LLM involved."""
    eng = DFMRuleEngine()
    geo = DFMGeometryResult(
        is_watertight=True, min_wall_thickness=0.3,  # < FDM 0.8mm critical
        overhang_ratio=0.0, material_ratio=0.5,
        bounding_box={"x_min": 0, "x_max": 50, "y_min": 0, "y_max": 50, "z_min": 0, "z_max": 50},
    )
    violations = await eng.evaluate(geo, process="FDM")
    ids = {v.rule_id for v in violations}
    assert "fdm_wall_thickness" in ids
    wall = next(v for v in violations if v.rule_id == "fdm_wall_thickness")
    assert wall.source == "geometric" and wall.severity == "critical"


@pytest.mark.asyncio
async def test_heuristic_rules_become_advisory_info():
    """Heuristic rules (undercut, tool access...) now emit advisory INFO notes,
    not LLM pass/fail. They must never be 'critical' from the engine anymore."""
    eng = DFMRuleEngine()
    geo = DFMGeometryResult(
        is_watertight=True, min_wall_thickness=5.0, overhang_ratio=0.0,
        material_ratio=0.9,
        bounding_box={"x_min": 0, "x_max": 50, "y_min": 0, "y_max": 50, "z_min": 0, "z_max": 50},
    )
    violations = await eng.evaluate(geo, process="injection_mold")
    heuristics = [v for v in violations if v.source == "heuristic"]
    assert heuristics, "expected advisory heuristic notes"
    assert all(v.severity == "info" for v in heuristics)
    assert any("需人工确认" in v.message for v in heuristics)


@pytest.mark.asyncio
async def test_sla_drain_hole_detects_hollow_cavity():
    """The one heuristic with real geometric signal: hollow watertight part → drain warning."""
    eng = DFMRuleEngine()
    hollow = DFMGeometryResult(
        is_watertight=True, min_wall_thickness=2.0, overhang_ratio=0.0,
        material_ratio=0.3,  # < 0.6 → likely closed cavity
        bounding_box={"x_min": 0, "x_max": 50, "y_min": 0, "y_max": 50, "z_min": 0, "z_max": 50},
    )
    violations = await eng.evaluate(hollow, process="SLA")
    drain = [v for v in violations if v.rule_id == "sla_drain_hole"]
    assert drain and drain[0].severity == "warning"


# === full analyzer end-to-end on a real STL, no key ===

@pytest.mark.asyncio
async def test_analyzer_end_to_end_no_llm():
    """DFMAnalyzer.analyze on a real thin box → deterministic score + violations,
    with NO dashscope key set. Proves the LLM is fully removed."""
    from app.validation.dfm_analyzer import DFMAnalyzer

    mesh = trimesh.creation.box(extents=(40, 40, 0.5))  # thin → FDM wall issue
    f = tempfile.NamedTemporaryFile(suffix=".stl", delete=False)
    mesh.export(f.name)

    analyzer = DFMAnalyzer()
    result = await analyzer.analyze(Path(f.name), process="FDM")

    assert result.design_score is None
    assert result.evaluation_status == "failed"
    assert any(check["status"] == "indeterminate" for check in result.evaluated_rules)
    assert result.design_summary  # template summary, non-empty
    assert result.rule_violations  # at least the wall-thickness violation
    assert result.structural_issues == []   # subjective VLM fields removed
    assert result.functional_notes == []
    # geometry attached
    assert result.geometry is not None and result.geometry.is_watertight
