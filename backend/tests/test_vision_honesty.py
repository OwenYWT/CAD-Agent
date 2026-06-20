"""Vision honesty (C2) — the visual self-check must never fail-open.

Before: no renders OR a parse failure returned is_match=True (silently "passed").
After: those return is_match=None (INDETERMINATE) — never a pass, never a fix retry,
and surfaced honestly as a 'vision' warn check on the inspect report.

Hermetic: real GeometryValidator, faked Docker/LLM/renderer.
"""
import pytest

from app.config import settings
from app.models.schemas import CADPlan, GenerationResult
from app.validation.vision_validator import VisionValidator, VisionValidationResult
from tests.e2e_harness import (
    FakeVision,
    FakeVisionResult,
    build_orchestrator,
    patch_single_step,
)


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    patch_single_step(monkeypatch)


def _plan(desc="测试零件"):
    return CADPlan(
        description=desc, part_type="custom", dimensions={},
        features=[], constraints=[], ambiguities=[], modeling_hint="extrude_cut",
    )


def _vision_check(report):
    return next((c for c in report.checks if c.name == "vision"), None)


# ============================================================================
# Unit: VisionValidator no longer fails open
# ============================================================================

@pytest.mark.asyncio
async def test_no_renders_is_indeterminate_not_pass():
    """REGRESSION GUARD: empty render list used to return is_match=True."""
    result = await VisionValidator().validate("一个盒子", [], "result = box(10,10)")
    assert result.is_match is None          # was True
    assert result.confidence == 0.0         # was 0.5
    assert result.is_match is not True       # must never read as a pass


def test_result_dataclass_allows_none():
    r = VisionValidationResult(is_match=None, confidence=0.0)
    assert r.is_match is None


# ============================================================================
# Integration: indeterminate is surfaced, never silently skipped or retried
# ============================================================================

@pytest.mark.asyncio
async def test_no_renders_adds_indeterminate_vision_check():
    """No render images → success stays True, but a 'vision' warn check is recorded
    and the verdict reflects it (warn) instead of a hidden pass."""
    orch = build_orchestrator(
        plan=_plan(desc="手机支架"),
        executor_outcomes=[{"success": True, "stl": "printable"}],
        renderer_paths=[],  # renderer produces nothing
    )
    r = await orch.generate("做一个手机支架")

    assert r.success is True                 # model was still produced
    vc = _vision_check(r.inspect_report)
    assert vc is not None and vc.status == "warn"
    assert vc.source == "vision"
    assert r.inspect_report.verdict == "warn"  # geometry passes, vision indeterminate


@pytest.mark.asyncio
async def test_indeterminate_vision_does_not_trigger_fix(tmp_path):
    """An is_match=None verdict must NOT call fix_visual_issues (only explicit False does)."""
    png = tmp_path / "iso.png"
    png.write_bytes(b"\x89PNG\r\n")
    orch = build_orchestrator(
        plan=_plan(),
        executor_outcomes=[{"success": True, "stl": "printable"}],
        renderer_paths=[png],
        vision=FakeVision(result=FakeVisionResult(is_match=None, issues=["解析失败"])),
    )
    r = await orch.generate("一个零件")

    assert r.success is True
    assert r.attempts == 1                   # no vision retry happened
    visual_fixes = [c for c in orch.code_gen.fix_calls if isinstance(c, dict) and c.get("type") == "visual"]
    assert visual_fixes == []                # fix_visual_issues was never called
    vc = _vision_check(r.inspect_report)
    assert vc is not None and vc.status == "warn"


@pytest.mark.asyncio
async def test_explicit_mismatch_still_triggers_fix(tmp_path):
    """Sanity: a real is_match=False still drives a visual fix (behavior preserved)."""
    png = tmp_path / "iso.png"
    png.write_bytes(b"\x89PNG\r\n")
    vision = FakeVision(result=FakeVisionResult(is_match=False, issues=["分离的部分"], suggestions=["合并"]))
    orch = build_orchestrator(
        plan=_plan(),
        executor_outcomes=[{"success": True, "stl": "printable"}],
        renderer_paths=[png],
        vision=vision,
    )

    async def flip(*a, **k):
        # after the first fix, vision passes
        vision.result = FakeVisionResult(is_match=True)
        return orch.code_gen.code

    orch.code_gen.fix_visual_issues = flip  # type: ignore
    r = await orch.generate("一个零件")
    assert r.success is True
    assert r.attempts >= 1


@pytest.mark.asyncio
async def test_passing_vision_keeps_verdict_pass(tmp_path):
    """A confirmed visual match adds no warn check; verdict stays pass."""
    png = tmp_path / "iso.png"
    png.write_bytes(b"\x89PNG\r\n")
    orch = build_orchestrator(
        plan=_plan(desc="手机支架"),
        executor_outcomes=[{"success": True, "stl": "printable"}],
        renderer_paths=[png],
        vision=FakeVision(result=FakeVisionResult(is_match=True)),
    )
    r = await orch.generate("做一个手机支架")
    assert r.inspect_report.verdict == "pass"
    assert _vision_check(r.inspect_report) is None
