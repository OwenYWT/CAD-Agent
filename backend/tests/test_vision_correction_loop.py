"""Vision verify-and-correct upgrades: dedicated vision model, kimi param shaping,
honest FAIL on exhausted fix budget, cache gating, and the local sandbox runtime.

Hermetic: no network, no containers (the one real-subprocess test is marked slow
and skips when cadquery isn't importable).
"""
import importlib.util

import pytest

from app.config import Settings, settings
from app.llm import build_chat_params
from app.models.schemas import CADPlan
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
# LLM param shaping for Moonshot K2 (fixed temperature, reasoning token floor)
# ============================================================================

def _settings(**kw):
    return Settings(_env_file=None, dashscope_api_key="test-key", **kw)


def test_kimi_drops_temperature_and_floors_max_tokens():
    params = build_chat_params(
        model="kimi-k2.5",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=1024,
        temperature=0.2,
        llm_settings=_settings(),
    )
    assert "temperature" not in params  # API rejects any value but the default
    assert params["max_tokens"] == 8192  # reasoning tokens share the completion cap


def test_kimi_large_max_tokens_not_reduced():
    params = build_chat_params(
        model="kimi-k2.5",
        messages=[],
        max_tokens=16384,
        temperature=0.1,
        llm_settings=_settings(),
    )
    assert params["max_tokens"] == 16384


def test_non_kimi_models_keep_temperature():
    params = build_chat_params(
        model="qwen-plus",
        messages=[],
        max_tokens=1024,
        temperature=0.2,
        llm_settings=_settings(),
    )
    assert params["temperature"] == 0.2
    assert params["max_tokens"] == 1024


# ============================================================================
# Dedicated vision model
# ============================================================================

def test_effective_vision_model_fallback():
    assert _settings(llm_model="text-model").effective_vision_model == "text-model"
    assert (
        _settings(llm_model="text-model", vision_model="vl-model").effective_vision_model
        == "vl-model"
    )


@pytest.mark.asyncio
async def test_vision_validator_uses_vision_model(monkeypatch, tmp_path):
    from app.validation import vision_validator as vv

    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)

            class Msg:
                content = '{"is_match": true, "confidence": 0.9, "issues": [], "suggestions": []}'

            class Choice:
                message = Msg()

            class Resp:
                choices = [Choice()]

            return Resp()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    validator = vv.VisionValidator()
    validator._client = FakeClient()
    monkeypatch.setattr(vv.settings, "vision_model", "vl-model-x")

    png = tmp_path / "iso.png"
    png.write_bytes(b"\x89PNG\r\n")
    res = await validator.validate("一个盒子", [png], "result = x")

    assert captured["model"] == "vl-model-x"
    assert res.is_match is True


# ============================================================================
# Exhausted vision budget → honest FAIL check, and the code is not cached
# ============================================================================

@pytest.mark.asyncio
async def test_persistent_mismatch_records_fail_and_skips_cache(tmp_path):
    png = tmp_path / "iso.png"
    png.write_bytes(b"\x89PNG\r\n")
    vision = FakeVision(result=FakeVisionResult(is_match=False, issues=["形状不匹配"]))
    orch = build_orchestrator(
        plan=_plan(),
        executor_outcomes=[{"success": True, "stl": "printable"}],
        renderer_paths=[png],
        vision=vision,
    )
    prompt = "一个零件"
    r = await orch.generate(prompt)

    assert r.success is True  # artifact still returned...
    vc = _vision_check(r.inspect_report)
    assert vc is not None and vc.status == "fail"  # ...but honestly marked failed
    assert r.inspect_report.verdict == "fail"
    # vision fixes were attempted up to the budget
    visual_fixes = [
        c for c in orch.code_gen.fix_calls
        if isinstance(c, dict) and c.get("type") == "visual"
    ]
    assert len(visual_fixes) == settings.vision_max_retries
    # visually-wrong code must not poison the prompt cache
    assert orch.code_cache.get(prompt) is None


@pytest.mark.asyncio
async def test_vision_pass_is_cached(tmp_path):
    png = tmp_path / "iso.png"
    png.write_bytes(b"\x89PNG\r\n")
    orch = build_orchestrator(
        plan=_plan(),
        executor_outcomes=[{"success": True, "stl": "printable"}],
        renderer_paths=[png],
        vision=FakeVision(result=FakeVisionResult(is_match=True)),
    )
    prompt = "一个零件"
    r = await orch.generate(prompt)

    assert r.success is True
    assert _vision_check(r.inspect_report) is None
    assert orch.code_cache.get(prompt) is not None


@pytest.mark.asyncio
async def test_mismatch_then_match_converges_without_fail_check(tmp_path):
    """One mismatch → one visual fix → match: no fail check, result cached."""
    png = tmp_path / "iso.png"
    png.write_bytes(b"\x89PNG\r\n")
    vision = FakeVision(result=FakeVisionResult(is_match=False, issues=["孔缺失"], suggestions=["加孔"]))
    orch = build_orchestrator(
        plan=_plan(),
        executor_outcomes=[{"success": True, "stl": "printable"}],
        renderer_paths=[png],
        vision=vision,
    )

    async def fix_and_pass(code, issues, suggestions, on_step=None):
        vision.result = FakeVisionResult(is_match=True)
        orch.code_gen.fix_calls.append({"type": "visual", "issues": issues})
        return orch.code_gen.code

    orch.code_gen.fix_visual_issues = fix_and_pass  # type: ignore
    prompt = "一个带孔的零件"
    r = await orch.generate(prompt)

    assert r.success is True
    assert _vision_check(r.inspect_report) is None
    assert orch.code_cache.get(prompt) is not None


# ============================================================================
# Local sandbox runtime
# ============================================================================

def test_local_runtime_dispatch(monkeypatch):
    from app.sandbox import executor as executor_module

    monkeypatch.setattr(executor_module.settings, "sandbox_runtime", "local")
    ex = executor_module.CadQueryExecutor()
    if importlib.util.find_spec("cadquery") is None:
        with pytest.raises(RuntimeError, match="cadquery"):
            _ = ex.client
    else:
        assert isinstance(ex.client, executor_module.LocalRuntime)


def test_unsupported_runtime_message_mentions_local(monkeypatch):
    from app.sandbox import executor as executor_module

    monkeypatch.setattr(executor_module.settings, "sandbox_runtime", "bogus")
    ex = executor_module.CadQueryExecutor()
    with pytest.raises(RuntimeError, match="local"):
        _ = ex.client


@pytest.mark.asyncio
async def test_local_runtime_blocks_native_escape(monkeypatch):
    """Local mode has no container, so the AST filter must gate it. A numpy->ctypes
    native-exec payload is rejected BEFORE any host subprocess runs."""
    from app.sandbox import executor as executor_module

    monkeypatch.setattr(executor_module.settings, "sandbox_runtime", "local")
    ex = executor_module.CadQueryExecutor()
    payload = (
        "import numpy as np\n"
        "np.ctypeslib.ctypes.CDLL('msvcrt').system(b'echo pwned')\n"
        "result = 1\nshow_object(result)"
    )
    result = await ex.execute(payload)
    assert result.success is False
    assert result.error_type == "ValidationError"
    import shutil

    shutil.rmtree(result.work_dir, ignore_errors=True)


def test_validate_code_blocks_numpy_ctypes_bridge():
    from app.sandbox.code_filter import validate_code

    for payload in [
        "import numpy as np\nx = np.ctypeslib.ctypes.CDLL('msvcrt')",
        "import numpy as np\nnp.ctypeslib.ctypes.windll.msvcrt.system(1)",
        "import numpy as np\nnp.zeros(3).tofile('C:/evil.bin')",
    ]:
        ok, err = validate_code(payload)
        assert ok is False, f"should have blocked: {payload}"


def test_validate_code_allows_normal_cadquery():
    from app.sandbox.code_filter import validate_code

    ok, err = validate_code(
        "import cadquery as cq\nimport numpy as np\n"
        "result = cq.Workplane('XY').box(10, 20, 30).faces('>Z').fillet(2)\n"
        "asm = cq.Assembly()\nasm.save('/sandbox/output/result.step')\n"
        "show_object(result)"
    )
    assert ok is True, err


@pytest.mark.asyncio
async def test_read_result_survives_corrupt_json(monkeypatch, tmp_path):
    """A truncated result.json (process killed mid-write) must yield a clean error
    SandboxResult, not an unhandled exception that leaks the work_dir + 500s."""
    from app.sandbox import executor as executor_module

    class CorruptRuntime:
        def run(self, input_dir, output_dir, timeout_s):
            (output_dir / "result.json").write_text('{"status": "succ')  # truncated
            return 137, "", "Killed"

    monkeypatch.setattr(executor_module.settings, "sandbox_runtime", "local")
    ex = executor_module.CadQueryExecutor()
    ex._client = CorruptRuntime()
    result = ex._execute_sync("result = 1\nshow_object(result)", mode="3d")
    assert result.success is False
    assert result.work_dir is not None  # caller can still clean up
    assert "result.json" in (result.error_message or "")
    import shutil

    shutil.rmtree(result.work_dir, ignore_errors=True)


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.skipif(
    importlib.util.find_spec("cadquery") is None,
    reason="cadquery not installed on host",
)
async def test_local_runtime_executes_real_cadquery(monkeypatch):
    """End-to-end host-subprocess execution: code in → STL/STEP + result.json out.
    Includes a Chinese comment to exercise the UTF-8 read path in local mode."""
    from app.sandbox import executor as executor_module

    monkeypatch.setattr(executor_module.settings, "sandbox_runtime", "local")
    monkeypatch.setattr(executor_module.settings, "sandbox_timeout_s", 180)
    ex = executor_module.CadQueryExecutor()
    result = await ex.execute(
        "# 创建一个 5×5×5 的盒子——含中文注释\n"
        "result = cq.Workplane('XY').box(5, 5, 5)\nshow_object(result)"
    )
    assert result.success, f"{result.error_type}: {result.error_message}"
    assert any(name.endswith(".stl") for name in result.files)
    assert any(name.endswith(".step") for name in result.files)
    import shutil

    shutil.rmtree(result.work_dir, ignore_errors=True)
