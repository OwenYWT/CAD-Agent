"""Hermetic PIPELINE BRANCH MATRIX tests for the CAD Agent.

Exercises the REAL Orchestrator + REAL GeometryValidator end-to-end via the
fake-executor harness (tests/e2e_harness.build_orchestrator), covering branch
combinations NOT already in test_e2e_pipeline.py:

  1. modify() with 2D (ezdxf) original code  vs  3D original code
  2. execute_code with valid printable code -> success + validation
  3. generate output_formats=['stl'] only  vs  ['step','stl','dxf']
  4. assembly with a failing part that recovers via fix
  5. multi-step path (PlanDecomposer -> complexity!=simple, 2 steps)
  6. geometry validation retry: non_watertight then printable (via modify)
  7. handle_message stateful: generate then modify carries current_code
  8. cache hit path (second identical prompt skips plan+codegen)
  9. _detect_intent edge cases (selection context, relative keywords, fresh-start)

No Docker, no API key. Run:
  python3 -m pytest tests/test_deploy_pipeline_matrix.py -p no:cacheprovider -o addopts="" -q
"""
import pytest

from app.config import settings
from app.models.schemas import CADPlan, ModificationPlan
from tests.e2e_harness import build_orchestrator, patch_single_step


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Point file storage at a temp dir + force the single-step path by default.
    Tests that want the multi-step path re-patch PlanDecomposer.decompose themselves."""
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    patch_single_step(monkeypatch)


def _plan(part_type="custom", hint="extrude_cut", dims=None, desc="测试零件"):
    return CADPlan(
        description=desc, part_type=part_type, dimensions=dims or {},
        features=[], constraints=[], ambiguities=[], modeling_hint=hint,
    )


# ============================================================================
# 1. modify() 2D (ezdxf) vs 3D
# ============================================================================

@pytest.mark.asyncio
async def test_modify_2d_original_code_runs_in_2d_mode_and_returns_dxf():
    """modify() detects 2D from the ORIGINAL code (ezdxf marker), runs the executor
    in 2d mode, and returns via the DXF early-return (no geometry validation)."""
    orch = build_orchestrator(
        mod_plan=ModificationPlan(description="加大轮廓", modification_type="dimension_change"),
        executor_outcomes=[{"success": True, "dxf": True}],
    )
    original_2d = "import ezdxf\ndoc = ezdxf.new()\ndoc.saveas('/sandbox/output/result.dxf')"
    r = await orch.modify(original_2d, "把垫片轮廓加大")
    assert r.success is True
    assert "dxf" in r.files
    # 2D path returns before geometry validation
    assert r.validation is None
    # executor was driven in 2d mode (proves is_2d detection on the original code)
    assert orch.executor.calls[-1]["mode"] == "2d"


@pytest.mark.asyncio
async def test_modify_3d_original_code_runs_in_3d_mode_and_validates():
    """modify() with 3D original code runs in 3d mode and produces real geometry validation."""
    orch = build_orchestrator(
        mod_plan=ModificationPlan(description="加大", modification_type="dimension_change"),
        code="w = 50  # 宽\nresult = x\nshow_object(result)",
        executor_outcomes=[{"success": True, "stl": "printable"}],
    )
    r = await orch.modify("w = 20\nresult = x\nshow_object(result)", "把宽度加大到 50")
    assert r.success is True
    assert r.validation is not None
    assert r.validation.printable is True
    assert orch.executor.calls[-1]["mode"] == "3d"
    assert {"stl", "step"} <= set(r.files)


# ============================================================================
# 2. execute_code valid printable -> success + validation (real geometry gate)
# ============================================================================

@pytest.mark.asyncio
async def test_execute_code_valid_printable_validates_geometry():
    """execute_code runs code directly AND now runs the printability gate (parity
    with /api/generate), so parameter edits get watertight/printable feedback."""
    orch = build_orchestrator(
        executor_outcomes=[{"success": True, "stl": "printable"}],
    )
    code = "w = 20  # 宽\nh = 30  # 高\nresult = x\nshow_object(result)"
    r = await orch.execute_code(code)
    assert r.success is True
    assert r.attempts == 1
    assert {"stl", "step"} <= set(r.files)
    # execute_code now returns a ValidationResult with the printability verdict
    assert r.validation is not None
    assert r.validation.printable is True
    assert r.validation.is_watertight is True
    # real _extract_params parsed the variable assignments
    assert r.params is not None and r.params["w"].value == 20


# ============================================================================
# 3. output_formats: ['stl'] only  vs  ['step','stl','dxf']
# ============================================================================

@pytest.mark.asyncio
async def test_generate_stl_only_still_copies_step_alongside():
    """Requesting only 'stl' still surfaces the STEP produced alongside it
    (copy_output_files' 'also copy other formats' pass). Validation still printable."""
    orch = build_orchestrator(
        plan=_plan(desc="支架"),
        executor_outcomes=[{"success": True, "stl": "printable"}],
    )
    r = await orch.generate("做一个支架", output_formats=["stl"])
    assert r.success is True
    assert "stl" in r.files
    # STEP is written by the fake executor and copied even though only stl was requested
    assert "step" in r.files
    assert r.validation.printable is True


@pytest.mark.asyncio
async def test_generate_step_stl_dxf_formats_requested():
    """A 3D part producing stl+step: requesting step/stl/dxf yields step+stl
    (no dxf is produced by a 3D run, so it's simply absent — not an error)."""
    orch = build_orchestrator(
        plan=_plan(desc="块"),
        executor_outcomes=[{"success": True, "stl": "printable"}],
    )
    r = await orch.generate("一个方块", output_formats=["step", "stl", "dxf"])
    assert r.success is True
    assert {"stl", "step"} <= set(r.files)
    assert "dxf" not in r.files  # 3D run produced no dxf
    assert r.validation.printable is True


# ============================================================================
# 4. assembly with a failing part that recovers via fix
# ============================================================================

@pytest.mark.asyncio
async def test_assembly_part_fails_then_recovers_via_fix(monkeypatch):
    """One part fails its first execute, fix_error is called, retry succeeds ->
    part status 'success', no failed parts, and the assembly is built."""
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

    # executor call order in assembly path:
    #   1. base part execute      -> success
    #   2. lid part execute       -> FAIL (triggers fix_error)
    #   3. lid retry execute       -> success
    #   4. final combined execute -> success (then geometry validation)
    orch = build_orchestrator(
        plan=_plan(part_type="assembly", hint="boolean_combine", desc="带盖盒子装配"),
        executor_outcomes=[
            {"success": True, "stl": "printable"},
            {"success": False, "error_type": "BRep_API", "error_message": "lid broke"},
            {"success": True, "stl": "printable"},
            {"success": True, "stl": "printable"},
        ],
    )
    r = await orch.generate("一个带盖的盒子装配体")
    assert r.success is True
    assert r.assembly_parts is not None
    assert [p.name for p in r.assembly_parts] == ["base", "lid"]
    # the failing part recovered -> both parts report success
    assert all(p.status == "success" for p in r.assembly_parts)
    # fix_error was invoked for the failing part
    assert len(orch.code_gen.fix_calls) >= 1


@pytest.mark.asyncio
async def test_assembly_part_fails_and_stays_failed(monkeypatch):
    """A part that fails BOTH its initial execute and its retry is flagged status
    'failed', but the assembly still builds (best-effort) and overall success holds."""
    from app.agent import assembly_planner

    async def _fake_plan_assembly(self, plan):
        return assembly_planner.AssemblyPlan(
            assembly_description="盒子",
            parts=[
                assembly_planner.AssemblyPart(name="base", description="底座", dimensions={}),
                assembly_planner.AssemblyPart(name="lid", description="盖子", dimensions={}),
            ],
        )
    monkeypatch.setattr(assembly_planner.AssemblyPlanner, "plan_assembly", _fake_plan_assembly)

    # base ok; lid fails twice (initial + retry); final combined succeeds.
    orch = build_orchestrator(
        plan=_plan(part_type="assembly", hint="boolean_combine", desc="盒子装配"),
        executor_outcomes=[
            {"success": True, "stl": "printable"},
            {"success": False, "error_type": "RuntimeError", "error_message": "lid x"},
            {"success": False, "error_type": "RuntimeError", "error_message": "lid x"},
            {"success": True, "stl": "printable"},
        ],
    )
    r = await orch.generate("一个盒子装配体")
    assert r.success is True
    statuses = {p.name: p.status for p in r.assembly_parts}
    assert statuses["base"] == "success"
    assert statuses["lid"] == "failed"


# ============================================================================
# 5. multi-step path (complexity != simple, 2 steps)
# ============================================================================

def _patch_multi_step(monkeypatch, code_gen, *, steps=2, complexity="moderate"):
    """Force the multi-step branch: PlanDecomposer.decompose returns >1 step with
    complexity != 'simple', and give the fake code_gen a generate_step method
    (MultiStepExecutor calls it; the harness FakeCodeGen lacks it)."""
    from app.agent import multi_step

    async def _fake_decompose(self, plan):
        return multi_step.BuildPlan(
            steps=[
                multi_step.BuildStep(phase=multi_step.BuildPhase.BASE, description="基础形体"),
                multi_step.BuildStep(phase=multi_step.BuildPhase.PRIMARY, description="主要特征"),
            ][:steps],
            complexity=complexity,
        )
    monkeypatch.setattr(multi_step.PlanDecomposer, "decompose", _fake_decompose)

    async def _gen_step(step_description, accumulated_code, step_index, total_steps, plan_context=""):
        # each step contributes a trivial line; result must remain a single entity
        return f"result = part_{step_index}  # step {step_index}"
    code_gen.generate_step = _gen_step


@pytest.mark.asyncio
async def test_multi_step_path_two_steps_success(monkeypatch):
    """complexity='moderate' + 2 steps routes through MultiStepExecutor.execute_plan;
    final execute succeeds -> attempts == number of steps, geometry validated."""
    orch = build_orchestrator(
        plan=_plan(desc="复杂支架"),
        # MultiStepExecutor runs: step1 execute, step2 execute, final execute = 3 calls
        executor_outcomes=[
            {"success": True, "stl": "printable"},
            {"success": True, "stl": "printable"},
            {"success": True, "stl": "printable"},
        ],
    )
    _patch_multi_step(monkeypatch, orch.code_gen, steps=2, complexity="moderate")
    r = await orch.generate("一个复杂的多特征支架")
    assert r.success is True
    # MultiStepExecutor reports attempts == total steps
    assert r.attempts == 2
    assert r.validation is not None
    assert r.validation.is_watertight is True
    assert {"stl", "step"} <= set(r.files)


@pytest.mark.asyncio
async def test_multi_step_final_execution_failure(monkeypatch):
    """If the accumulated final code fails to execute, the multi-step path returns
    success=False with the executor's error type."""
    orch = build_orchestrator(
        plan=_plan(desc="复杂件"),
        # step1 ok, step2 ok, final fails
        executor_outcomes=[
            {"success": True, "stl": "printable"},
            {"success": True, "stl": "printable"},
            {"success": False, "error_type": "BRep_API", "error_message": "final boom"},
        ],
    )
    _patch_multi_step(monkeypatch, orch.code_gen, steps=2, complexity="complex")
    r = await orch.generate("一个会失败的复杂件")
    assert r.success is False
    assert r.error["type"] == "BRep_API"
    assert r.attempts == 2


# ============================================================================
# 6. geometry validation retry: non_watertight then printable (via modify)
# ============================================================================

@pytest.mark.asyncio
async def test_modify_non_watertight_then_fixed_to_printable():
    """Through modify(): first STL non-watertight (geometry gate fails -> fix_error),
    second printable -> success. Proves the geometry retry loop runs on the modify path."""
    orch = build_orchestrator(
        mod_plan=ModificationPlan(description="修复", modification_type="dimension_change"),
        code="w = 20\nresult = x\nshow_object(result)",
        executor_outcomes=[
            {"success": True, "stl": "non_watertight"},  # attempt 1: geometry fails
            {"success": True, "stl": "printable"},         # attempt 2: passes
        ],
        fix_queue=["w = 10  # fixed\nresult = x\nshow_object(result)"],
    )
    r = await orch.modify("w = 20\nresult = x\nshow_object(result)", "修复破面")
    assert r.success is True
    assert r.attempts == 2
    assert r.validation.printable is True
    assert r.validation.is_watertight is True
    # exactly one geometry-driven fix
    assert len(orch.code_gen.fix_calls) == 1
    assert orch.code_gen.fix_calls[0]["type"] == "GeometryError"


# ============================================================================
# 7. handle_message stateful: generate then modify carries current_code
# ============================================================================

@pytest.mark.asyncio
async def test_handle_message_generate_then_modify_carries_context():
    """First message (no code) -> generate, current_code populated. Second message
    with a modify keyword -> modify path, using the carried current_code."""
    from app.agent.orchestrator import ConversationContext

    orch = build_orchestrator(
        plan=_plan(desc="盒子"),
        mod_plan=ModificationPlan(description="加高", modification_type="dimension_change"),
        code="w = 20  # 宽\nresult = x\nshow_object(result)",
        executor_outcomes=[{"success": True, "stl": "printable"}],
    )
    ctx = ConversationContext(session_id="s-stateful")

    r1 = await orch.handle_message(ctx, "做一个盒子")
    assert r1.success is True
    assert ctx.current_code is not None
    assert ctx.generation_count == 1
    code_after_gen = ctx.current_code

    # second turn: modify keyword + existing code -> modify path
    r2 = await orch.handle_message(ctx, "把高度加高一点")
    assert r2.success is True
    assert ctx.generation_count == 2
    # context still has code (re-derived from the modify response)
    assert ctx.current_code is not None
    # modify() ran the executor a second time on the modify path
    assert len(orch.executor.calls) == 2
    _ = code_after_gen  # documented: code carried into the modify turn


# ============================================================================
# 8. cache hit path (second identical prompt)
# ============================================================================

@pytest.mark.asyncio
async def test_cache_hit_skips_plan_and_codegen():
    """Second identical prompt hits the code cache: planner.plan_new is NOT called
    again and code_gen.generate is NOT called again; cached code re-executes fresh."""
    orch = build_orchestrator(
        plan=_plan(desc="缓存件"),
        code="w = 12  # 缓存\nresult = x\nshow_object(result)",
        executor_outcomes=[{"success": True, "stl": "printable"}],
    )

    # count planner calls
    plan_calls = {"n": 0}
    orig_plan_new = orch.planner.plan_new

    async def _counting_plan_new(messages):
        plan_calls["n"] += 1
        return await orig_plan_new(messages)
    orch.planner.plan_new = _counting_plan_new

    r1 = await orch.generate("一个完全相同的提示词")
    assert r1.success is True
    assert plan_calls["n"] == 1
    gen_calls_after_first = orch.code_gen.generate_calls
    assert gen_calls_after_first == 1

    r2 = await orch.generate("一个完全相同的提示词")
    assert r2.success is True
    # cache hit: no new plan, no new codegen
    assert plan_calls["n"] == 1
    assert orch.code_gen.generate_calls == gen_calls_after_first
    # the cached code was re-executed (two successful executor runs total)
    assert orch.code_cache.hits == 1
    assert r2.code == r1.code


@pytest.mark.asyncio
async def test_cache_miss_on_different_prompt():
    """A different prompt does NOT hit the cache -> plan + codegen run again."""
    orch = build_orchestrator(
        plan=_plan(desc="件"),
        executor_outcomes=[{"success": True, "stl": "printable"}],
    )
    await orch.generate("第一个提示")
    assert orch.code_gen.generate_calls == 1
    await orch.generate("第二个不同的提示")
    assert orch.code_gen.generate_calls == 2
    assert orch.code_cache.hits == 0


# ============================================================================
# 9. _detect_intent edge cases
# ============================================================================

def test_detect_intent_selection_relative_keyword_without_code():
    """[Selection Context] + a relative keyword (with NO current code) ->
    generate_relative (selection-aware new generation)."""
    from app.agent.orchestrator import ConversationContext
    orch = build_orchestrator()
    ctx = ConversationContext(session_id="s-rel")
    msg = "[Selection Context] 根据选中的零件做一个配套的盖子"
    assert orch._detect_intent(msg, ctx) == "generate_relative"


def test_detect_intent_selection_plus_code_defaults_to_modify():
    """Existing code + [Selection Context] but no explicit keyword -> modify."""
    from app.agent.orchestrator import ConversationContext
    orch = build_orchestrator()
    ctx = ConversationContext(session_id="s-sel")
    ctx.current_code = "result = x"
    # no modify/relative/generate keyword, but has code + selection
    assert orch._detect_intent("[Selection Context] 这个面", ctx) == "modify"


def test_detect_intent_fresh_start_overrides_modify_keyword():
    """A fresh-start marker beats a modify keyword even with existing code ->
    '重新生成' wins over '改'."""
    from app.agent.orchestrator import ConversationContext
    orch = build_orchestrator()
    ctx = ConversationContext(session_id="s-fresh")
    ctx.current_code = "result = x"
    assert orch._detect_intent("重新生成一个，把宽度改大", ctx) == "generate"


def test_detect_intent_modify_keyword_requires_existing_code():
    """A modify keyword with NO current code falls through to generate
    (can't modify what doesn't exist yet)."""
    from app.agent.orchestrator import ConversationContext
    orch = build_orchestrator()
    ctx = ConversationContext(session_id="s-nocode")
    # '加厚' is a modify keyword, but no current_code -> generate
    assert orch._detect_intent("把它加厚", ctx) == "generate"


def test_detect_intent_relative_keyword_requires_selection():
    """A relative keyword ('配套') WITHOUT a [Selection Context] is not
    generate_relative — it falls through to plain generate."""
    from app.agent.orchestrator import ConversationContext
    orch = build_orchestrator()
    ctx = ConversationContext(session_id="s-norel")
    assert orch._detect_intent("做一个配套的支架", ctx) == "generate"
