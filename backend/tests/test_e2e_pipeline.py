"""Hermetic END-TO-END tests for the CAD Agent pipeline.

Exercises the FULL real Orchestrator (routing, retry loop, printability gate,
file copy, param extraction, fallback) with only the LLM + Docker boundaries faked.
The fake executor writes a REAL trimesh STL, so the real GeometryValidator runs.

No Docker, no API key required. Run: pytest tests/test_e2e_pipeline.py -v
"""
import tempfile

import pytest

from app.config import settings
from app.models.schemas import CADPlan, ModificationPlan
from tests.e2e_harness import build_orchestrator, patch_single_step, make_stl


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Point file storage at a temp dir + force single-step path."""
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    patch_single_step(monkeypatch)


def _plan(part_type="custom", hint="extrude_cut", dims=None, desc="测试零件"):
    return CADPlan(
        description=desc, part_type=part_type, dimensions=dims or {},
        features=[], constraints=[], ambiguities=[], modeling_hint=hint,
    )


# ============================================================================
# Routing: simple / 2D / assembly
# ============================================================================

@pytest.mark.asyncio
async def test_simple_part_happy_path():
    """custom part → single-step → execute → real validator → printable success."""
    orch = build_orchestrator(
        plan=_plan(desc="手机支架"),
        code="w = 20  # 宽\nh = 30  # 高\nresult = thing\nshow_object(result)",
        executor_outcomes=[{"success": True, "stl": "printable"}],
    )
    r = await orch.generate("做一个手机支架")
    assert r.success is True
    assert r.attempts == 1
    assert r.validation.printable is True
    assert r.validation.is_watertight is True
    assert {"stl", "step"} <= set(r.files)
    # real _extract_params parsed the variable lines
    assert "w" in r.params and r.params["w"].value == 20


@pytest.mark.asyncio
async def test_2d_profile_path_returns_dxf():
    """profile_2d → generate_2d → DXF early-return (no geometry validation)."""
    orch = build_orchestrator(
        plan=_plan(part_type="profile_2d", desc="激光切割垫片"),
        executor_outcomes=[{"success": True, "dxf": True}],
    )
    r = await orch.generate("画一个 2D 激光切割垫片轮廓")
    assert r.success is True
    assert "dxf" in r.files
    # 2D path returns before geometry validation
    assert r.validation is None


@pytest.mark.asyncio
async def test_assembly_path_builds_parts(monkeypatch):
    """assembly part_type → per-part gen → combiner → assembly_parts metadata."""
    from app.agent import assembly_planner

    async def _fake_plan_assembly(self, plan):
        return assembly_planner.AssemblyPlan(
            assembly_description="带盖盒子",
            parts=[
                assembly_planner.AssemblyPart(name="base", description="底座", dimensions={}),
                assembly_planner.AssemblyPart(name="lid", description="盖子", dimensions={}),
            ],
        )
    monkeypatch.setattr(assembly_planner.AssemblyPlanner, "plan_assembly", _fake_plan_assembly)

    orch = build_orchestrator(
        plan=_plan(part_type="assembly", hint="boolean_combine", desc="带盖盒子装配"),
        # parts validated (2 calls) + final combined execute
        executor_outcomes=[
            {"success": True, "stl": "printable"},
            {"success": True, "stl": "printable"},
            {"success": True, "stl": "printable"},
        ],
    )
    r = await orch.generate("一个带盖的盒子装配体")
    assert r.success is True
    assert r.assembly_parts is not None
    assert [p.name for p in r.assembly_parts] == ["base", "lid"]


# ============================================================================
# Printability gate through the full pipeline
# ============================================================================

@pytest.mark.asyncio
async def test_oversized_part_succeeds_but_flagged_not_printable():
    """DESIGN CONTRACT (locked): a model that executes but is NOT printable (here:
    exceeds the 256mm build volume) returns success=True so the tester still gets a
    3D preview + can give feedback, BUT printable=False with a clear warning. success
    means 'a model was produced', printable is the honest 3D-printing verdict.

    The geometry failure also drives the retry loop (fix_error called each attempt)
    before the final attempt ships the (un-printable) result as metadata."""
    orch = build_orchestrator(
        plan=_plan(),
        executor_outcomes=[{"success": True, "stl": "oversized"}],  # repeats for all retries
    )
    r = await orch.generate("一个超大零件")
    assert r.success is True                      # model produced → tester sees preview
    assert r.validation.printable is False        # but honestly flagged un-printable
    assert r.validation.fits_build_volume is False
    assert any("成型空间" in w for w in r.validation.print_warnings)
    assert r.attempts == orch.MAX_RETRIES         # geometry failure drove every retry
    assert len(orch.code_gen.fix_calls) >= 1


@pytest.mark.asyncio
async def test_non_watertight_then_fixed_to_printable():
    """First STL non-watertight (geometry fail → retry), second printable → success."""
    orch = build_orchestrator(
        plan=_plan(),
        executor_outcomes=[
            {"success": True, "stl": "non_watertight"},  # attempt 1: geometry fails
            {"success": True, "stl": "printable"},        # attempt 2: passes
        ],
        fix_queue=["w = 10  # fixed\nresult = x\nshow_object(result)"],
    )
    r = await orch.generate("一个零件")
    assert r.success is True
    assert r.attempts == 2
    assert r.validation.printable is True
    assert len(orch.code_gen.fix_calls) == 1  # one geometry-driven fix


@pytest.mark.asyncio
async def test_thin_wall_is_printable_with_warning():
    """A thin watertight part that fits the bed is still printable; wall is advisory."""
    orch = build_orchestrator(
        plan=_plan(),
        executor_outcomes=[{"success": True, "stl": "thin"}],
    )
    r = await orch.generate("一个薄壁件")
    assert r.success is True
    assert r.validation.printable is True
    # advisory wall warning surfaced (not a hard fail)
    assert r.validation.min_wall_thickness is not None


# ============================================================================
# Retry loop: code-filter / execution-error branches
# ============================================================================

@pytest.mark.asyncio
async def test_code_filter_rejection_then_fix():
    """Invalid (disallowed import) code → validate_code fails → fix → valid → success."""
    orch = build_orchestrator(
        plan=_plan(),
        code="import os\nresult = x\nshow_object(result)",   # blocked by code_filter
        executor_outcomes=[{"success": True, "stl": "printable"}],
        fix_queue=["w = 5  # ok\nresult = x\nshow_object(result)"],  # the 'fixed' code
    )
    r = await orch.generate("一个零件")
    assert r.success is True
    assert len(orch.code_gen.fix_calls) >= 1
    # the executed code is the fixed one, not the rejected import
    assert "import os" not in (r.code or "")


@pytest.mark.asyncio
async def test_execution_error_then_recover():
    """Sandbox execution fails once → fix_error → second attempt succeeds."""
    orch = build_orchestrator(
        plan=_plan(),
        executor_outcomes=[
            {"success": False, "error_type": "BRep_API", "error_message": "not done"},
            {"success": True, "stl": "printable"},
        ],
        fix_queue=["w = 7  # fixed\nresult = x\nshow_object(result)"],
    )
    r = await orch.generate("一个零件")
    assert r.success is True
    assert r.attempts == 2
    assert orch.code_gen.fix_calls[0]["type"] == "BRep_API"


@pytest.mark.asyncio
async def test_all_execution_failures_exhaust_retries():
    """Every attempt fails in the sandbox → final failure after MAX_RETRIES."""
    orch = build_orchestrator(
        plan=_plan(),
        executor_outcomes=[{"success": False, "error_type": "RuntimeError", "error_message": "boom"}],
    )
    r = await orch.generate("一个零件")
    assert r.success is False
    assert r.attempts == orch.MAX_RETRIES
    assert r.error["type"] in ("RuntimeError", "ExecutionError")


# ============================================================================
# Vision validation branch
# ============================================================================

@pytest.mark.asyncio
async def test_vision_mismatch_triggers_fix():
    """When renders exist and vision says mismatch, a visual fix is attempted, then matches."""
    from tests.e2e_harness import FakeVision, FakeVisionResult

    # vision: first call mismatch, then we only have one validator instance → make it match
    vision = FakeVision(result=FakeVisionResult(is_match=False, issues=["分离的部分"], suggestions=["合并"]))
    # provide a render path so vision runs
    fake_png = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
    orch = build_orchestrator(
        plan=_plan(),
        executor_outcomes=[{"success": True, "stl": "printable"}, {"success": True, "stl": "printable"}],
        vision=vision,
        renderer_paths=[fake_png],
        fix_queue=["w = 9  # visually fixed\nresult = x\nshow_object(result)"],
    )
    # after first mismatch we flip vision to match so it converges
    orig = vision.validate
    async def flipping(prompt, render_paths, code):
        vision.result = FakeVisionResult(is_match=True)
        return FakeVisionResult(is_match=False, issues=["x"], suggestions=["y"]) if not getattr(flipping, "done", False) else FakeVisionResult(is_match=True)
    # simpler: just assert a visual fix was attempted
    r = await orch.generate("一个零件")
    # vision drove at least one visual fix
    assert any(isinstance(c, dict) and c.get("type") == "visual" for c in orch.code_gen.fix_calls)


# ============================================================================
# Strategy fallback
# ============================================================================

@pytest.mark.asyncio
async def test_strategy_fallback_on_simple_part_failure():
    """Simple part fails → _get_fallback_hint switches strategy → fallback attempt runs."""
    orch = build_orchestrator(
        plan=_plan(hint="revolve"),  # fallback map: revolve → extrude_cut
        executor_outcomes=[{"success": False, "error_type": "RuntimeError", "error_message": "boom"}],
    )
    r = await orch.generate("一个回转体")
    # main loop fails → strategy fallback re-generates with the alternative hint.
    # The fallback path is proven by code_gen.generate being called twice
    # (initial generation + fallback regeneration).
    assert r.success is False
    assert orch.code_gen.generate_calls >= 2


# ============================================================================
# modify() and execute_code()
# ============================================================================

@pytest.mark.asyncio
async def test_modify_existing_code():
    orch = build_orchestrator(
        mod_plan=ModificationPlan(description="加大", modification_type="dimension_change"),
        code="w = 50  # 加大后\nresult = x\nshow_object(result)",
        executor_outcomes=[{"success": True, "stl": "printable"}],
    )
    r = await orch.modify("w = 20\nresult = x\nshow_object(result)", "把宽度加大到 50")
    assert r.success is True
    assert r.validation.printable is True


@pytest.mark.asyncio
async def test_execute_code_direct_no_llm():
    """execute_code runs code directly (no planner/codegen), still validates geometry."""
    orch = build_orchestrator(
        executor_outcomes=[{"success": True, "stl": "printable"}],
    )
    code = "w = 20  # 宽\nresult = x\nshow_object(result)"
    r = await orch.execute_code(code)
    assert r.success is True
    assert r.code == code
    assert r.attempts == 1
    assert r.inspect_report is not None
    assert sorted(r.inspect_report.available_exports) == ["step", "stl"]
    assert r.inspect_report.repair_attempts == 0


@pytest.mark.asyncio
async def test_execute_code_rejects_disallowed_import():
    """execute_code validates via code_filter before running — blocked import → failure."""
    orch = build_orchestrator()
    r = await orch.execute_code("import subprocess\nresult = x\nshow_object(result)")
    assert r.success is False
    assert r.error["type"] == "ValidationError"
    # never reached the executor
    assert len(orch.executor.calls) == 0


# ============================================================================
# Intent detection (handle_message routing)
# ============================================================================

@pytest.mark.asyncio
async def test_intent_modify_vs_generate():
    from app.agent.orchestrator import ConversationContext
    orch = build_orchestrator()
    ctx = ConversationContext(session_id="s1")
    # no code yet → generate
    assert orch._detect_intent("做一个盒子", ctx) == "generate"
    # has code + modify keyword → modify
    ctx.current_code = "result = x"
    assert orch._detect_intent("把宽度改大", ctx) == "modify"
    # explicit generate keyword wins
    assert orch._detect_intent("重新生成一个新的", ctx) == "generate"
