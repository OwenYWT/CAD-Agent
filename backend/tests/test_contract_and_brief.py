"""C3 (manufacture-realistic contract + numeric clarity) and A2 (CADPlan brief on
responses) — verify the prompt contract text is present, the wall/size numbers are
disambiguated (design target vs hard limit), and the plan brief flows onto responses.

Hermetic: no Docker/LLM; prompt is a static string, orchestrator faked.
"""
import pytest

from app.agent.prompts import CODEGEN_SYSTEM_PROMPT
from app.config import settings
from app.models.schemas import CADPlan, GenerationResult
from tests.e2e_harness import build_orchestrator, patch_single_step


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    patch_single_step(monkeypatch)


def _plan(desc="测试零件", dims=None, feats=None, ambig=None):
    return CADPlan(
        description=desc, part_type="custom", dimensions=dims or {},
        features=feats or [], constraints=[], ambiguities=ambig or [],
        modeling_hint="extrude_cut",
    )


# ============================================================================
# C3: manufacture-realistic contract + numeric clarity
# ============================================================================

def test_prompt_has_manufacture_realistic_contract():
    s = CODEGEN_SYSTEM_PROMPT.format(examples="")
    assert "可制造原型" in s
    assert "manufacture-realistic" in s
    # the contract forbids decorative geometry
    assert "悬浮标签" in s or "铭牌" in s


def test_prompt_numeric_clarity_target_vs_hard_limit():
    """1.2mm/250mm are design TARGETS; 0.8mm/256mm are the hard limits — both stated,
    explicitly reconciled so the LLM doesn't see a contradiction."""
    s = CODEGEN_SYSTEM_PROMPT.format(examples="")
    assert "设计目标" in s
    assert "硬下限 0.8mm" in s
    assert "硬下限 256mm" in s


def test_prompt_hard_limits_match_config():
    """The hard-limit numbers quoted in the prompt must match the actual gate config."""
    s = CODEGEN_SYSTEM_PROMPT.format(examples="")
    assert f"硬下限 {settings.min_wall_mm:.1f}mm" in s        # 0.8mm
    assert f"硬下限 {settings.build_volume_mm:.0f}mm" in s     # 256mm


# ============================================================================
# A2: CADPlan brief on responses
# ============================================================================

@pytest.mark.asyncio
async def test_plan_brief_on_generate_response():
    orch = build_orchestrator(
        plan=_plan(desc="手机支架", dims={"w": 60.0, "h": 30.0}, feats=["底座", "卡槽"]),
        executor_outcomes=[{"success": True, "stl": "printable"}],
    )
    r = await orch.generate("做一个手机支架")
    assert r.plan is not None
    assert r.plan.description == "手机支架"
    assert r.plan.dimensions == {"w": 60.0, "h": 30.0}
    assert r.plan.features == ["底座", "卡槽"]


@pytest.mark.asyncio
async def test_plan_brief_flows_through_handle_message():
    from app.agent.orchestrator import ConversationContext
    orch = build_orchestrator(
        plan=_plan(desc="一个零件", ambig=["未指定厚度"]),
        executor_outcomes=[{"success": True, "stl": "printable"}],
    )
    ctx = ConversationContext(session_id="s1")
    result = await orch.handle_message(ctx, "做一个零件")
    assert isinstance(result, GenerationResult)
    assert result.plan is not None
    assert result.plan.ambiguities == ["未指定厚度"]


@pytest.mark.asyncio
async def test_plan_brief_present_even_when_not_printable():
    """The brief surfaces regardless of printability verdict (it's about understanding)."""
    orch = build_orchestrator(
        plan=_plan(desc="超大零件"),
        executor_outcomes=[{"success": True, "stl": "oversized"}],
    )
    r = await orch.generate("一个超大零件")
    assert r.success is True
    assert r.plan is not None
    assert r.plan.description == "超大零件"
