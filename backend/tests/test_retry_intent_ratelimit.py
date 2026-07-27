"""Tests for retry oscillation guard (#25), intent detection (#27),
rate limiter proxy handling (#30). Hermetic."""
import pytest
from types import SimpleNamespace

from app.config import settings
from app.models.schemas import CADPlan
from app.agent.multi_step import (
    BuildPhase,
    BuildPlan,
    BuildStep,
    MultiStepExecutor,
)
from app.agent.orchestrator import ConversationContext
from tests.e2e_harness import build_orchestrator, patch_single_step


@pytest.fixture(autouse=True)
def _storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    patch_single_step(monkeypatch)


# === #25 retry oscillation guard ===

@pytest.mark.asyncio
async def test_retry_stops_when_same_error_repeats():
    """Same execution error every attempt → stop early (tested on _execute_with_retry
    directly to isolate from the strategy-fallback re-run)."""
    orch = build_orchestrator(
        executor_outcomes=[{"success": False, "error_type": "BRep_API", "error_message": "not done"}],
        # fix returns DIFFERENT code each time so the "identical code" guard doesn't
        # fire first — proves the repeated-error-signature guard works.
        fix_queue=[f"v{i} = {i}\nresult = x\nshow_object(result)" for i in range(10)],
    )
    r = await orch._execute_with_retry(
        "rid", "w = 1\nresult = x\nshow_object(result)", None, ["stl"], None, "一个零件",
    )
    assert r.success is False
    # repeated-error guard breaks before exhausting MAX_RETRIES
    assert len(orch.executor.calls) < orch.MAX_RETRIES


@pytest.mark.asyncio
async def test_retry_stops_when_fix_returns_identical_code():
    """If fix_error returns the same code, retrying can't help → stop early."""
    same = "w = 1\nresult = x\nshow_object(result)"
    orch = build_orchestrator(
        executor_outcomes=[{"success": False, "error_type": "ExecutionError", "error_message": "boom"}],
        fix_queue=[same, same, same, same],  # fixer never changes anything
    )
    r = await orch._execute_with_retry("rid", same, None, ["stl"], None, "一个零件")
    assert r.success is False
    assert len(orch.executor.calls) < orch.MAX_RETRIES


@pytest.mark.asyncio
async def test_oscillation_key_normalized_on_failure_class():
    """A1: same FAILURE CLASS with a DIFFERENT message tail (varying coordinates) used
    to read as distinct signatures and never trip the guard. Now normalized on fc.key."""
    orch = build_orchestrator(
        # same OCCT class (Standard_NullObject) but a different coordinate each time
        executor_outcomes=[
            {"success": False, "error_type": "Standard_NullObject", "error_message": f"null at face ({i},{i},{i})"}
            for i in range(8)
        ],
        fix_queue=[f"v{i} = {i}\nresult = x\nshow_object(result)" for i in range(10)],
    )
    r = await orch._execute_with_retry("rid", "w = 1\nresult = x\nshow_object(result)", None, ["stl"], None, "零件")
    assert r.success is False
    # normalized key collapses the varying tails → guard trips before MAX_RETRIES
    assert len(orch.executor.calls) < orch.MAX_RETRIES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [
        "SandboxUnavailable",
        "ContainerLaunchError",
        "ArtifactRejected",
        "ExecutionCancelled",
    ],
)
async def test_hard_stop_execution_failures_make_one_attempt_and_no_llm_burn(
    error_type,
):
    orch = build_orchestrator(
        executor_outcomes=[{"success": False, "error_type": error_type, "error_message": "unrecoverable"}],
        fix_queue=[f"v{i} = {i}" for i in range(10)],
    )
    r = await orch._execute_with_retry("rid", "w = 1\nresult = x\nshow_object(result)", None, ["stl"], None, "零件")
    assert r.success is False
    assert r.attempts == 1
    assert len(orch.executor.calls) == 1
    visual_or_code_fixes = [c for c in orch.code_gen.fix_calls]
    assert visual_or_code_fixes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", ["ExecutionTimeout", "ExecutionOOM"])
async def test_bounded_resource_failure_gets_at_most_one_simplification(error_type):
    orch = build_orchestrator(
        executor_outcomes=[
            {"success": False, "error_type": error_type, "error_message": "resource limit"},
            {"success": False, "error_type": error_type, "error_message": "resource limit"},
        ],
        fix_queue=["w = 2\nresult = x\nshow_object(result)"],
    )

    result = await orch._execute_with_retry(
        "rid",
        "w = 1\nresult = x\nshow_object(result)",
        None,
        ["stl"],
        None,
        "零件",
    )

    assert result.success is False
    assert result.attempts == 2
    assert len(orch.executor.calls) == 2
    assert len(orch.code_gen.fix_calls) == 1


@pytest.mark.asyncio
async def test_multi_step_stops_on_infrastructure_failure_without_llm_fix(
    tmp_path,
):
    class CodeGen:
        def __init__(self):
            self.fix_calls = 0

        async def generate_step(self, *_args, **_kwargs):
            return "result = cq.Workplane('XY').box(1, 1, 1)"

        async def fix_error(self, *_args, **_kwargs):
            self.fix_calls += 1
            return "result = cq.Workplane('XY').box(2, 2, 2)"

    class Executor:
        def __init__(self):
            self.calls = 0

        async def execute(self, _code):
            self.calls += 1
            work_dir = tmp_path / f"attempt-{self.calls}"
            work_dir.mkdir()
            return SimpleNamespace(
                success=False,
                error_type="SandboxUnavailable",
                error_message="runtime unavailable",
                traceback=None,
                work_dir=work_dir,
                execution_time_ms=1,
            )

    code_gen = CodeGen()
    executor = Executor()
    multi_step = MultiStepExecutor(code_gen, executor)
    result = await multi_step.execute_plan(
        BuildPlan(
            steps=[
                BuildStep(
                    phase=BuildPhase.BASE,
                    description="创建基础块",
                )
            ],
            complexity="simple",
        ),
        CADPlan(
            description="基础块",
            part_type="box",
            dimensions={},
            features=[],
        ),
        [],
    )

    assert result.success is False
    assert result.error["type"] == "SandboxUnavailable"
    assert result.attempts == 1
    assert executor.calls == 1
    assert code_gen.fix_calls == 0


# === #27 intent detection ===

def test_intent_fresh_start_overrides_modify():
    orch = build_orchestrator()
    ctx = ConversationContext(session_id="s")
    ctx.current_code = "result = x"
    # has code + a modify word (改) BUT a fresh-start marker → should be generate
    assert orch._detect_intent("把这个删了，重新生成一个圆盘", ctx) == "generate"
    assert orch._detect_intent("换一个设计", ctx) == "generate"


def test_intent_plain_modify_still_modifies():
    orch = build_orchestrator()
    ctx = ConversationContext(session_id="s")
    ctx.current_code = "result = x"
    assert orch._detect_intent("把宽度改成 50", ctx) == "modify"


# === #30 rate limiter proxy handling ===

def test_rate_limiter_ignores_xff_by_default(monkeypatch):
    from app.api.auth import RateLimiter

    class Req:
        headers = {"x-forwarded-for": "9.9.9.9"}
        client = type("C", (), {"host": "10.0.0.1"})()

    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    rl = RateLimiter()
    assert rl._client_key(Req(), None) == "ip:10.0.0.1"


def test_rate_limiter_uses_xff_when_trusted(monkeypatch):
    from app.api.auth import RateLimiter

    class Req:
        headers = {"x-forwarded-for": "9.9.9.9, 10.0.0.1"}
        client = type("C", (), {"host": "10.0.0.1"})()

    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    rl = RateLimiter()
    assert rl._client_key(Req(), None) == "ip:9.9.9.9"  # first hop = real client
