"""Content-addressed code cache (#22): unit + e2e behavior. Hermetic."""
import pytest

from app.config import settings
from app.models.schemas import CADPlan
from app.agent.code_cache import CodeCache
from tests.e2e_harness import build_orchestrator, patch_single_step


# === unit ===

def test_cache_hit_miss_and_lru():
    c = CodeCache(max_entries=2)
    assert c.get("a") is None and c.misses == 1
    c.put("a", "code_a")
    assert c.get("a") == "code_a" and c.hits == 1
    c.put("b", "code_b")
    c.put("c", "code_c")           # evicts least-recently-used ("a")
    assert c.get("a") is None      # evicted
    assert c.get("c") == "code_c"


def test_cache_key_normalizes_whitespace():
    c = CodeCache()
    c.put("  make a box  ", "X")
    assert c.get("make a box") == "X"   # .strip() applied


# === e2e: second identical prompt skips LLM ===

@pytest.fixture(autouse=True)
def _storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    patch_single_step(monkeypatch)


@pytest.mark.asyncio
async def test_second_identical_prompt_uses_cache():
    orch = build_orchestrator(
        plan=CADPlan(description="盒子", part_type="custom", dimensions={}, features=[], modeling_hint="extrude_cut"),
        code="w = 10  # 宽\nresult = x\nshow_object(result)",
        executor_outcomes=[{"success": True, "stl": "printable"}],
    )
    r1 = await orch.generate("做一个盒子")
    assert r1.success
    gen_after_first = orch.code_gen.generate_calls
    assert gen_after_first == 1

    r2 = await orch.generate("做一个盒子")   # identical prompt
    assert r2.success
    # LLM codegen NOT called again on the cached prompt
    assert orch.code_gen.generate_calls == gen_after_first
    assert orch.code_cache.hits >= 1
    # still produced fresh files (re-executed)
    assert "stl" in r2.files


@pytest.mark.asyncio
async def test_different_prompt_does_not_use_cache():
    orch = build_orchestrator(
        plan=CADPlan(description="盒子", part_type="custom", dimensions={}, features=[], modeling_hint="extrude_cut"),
        code="w = 10\nresult = x\nshow_object(result)",
        executor_outcomes=[{"success": True, "stl": "printable"}],
    )
    await orch.generate("做一个盒子")
    await orch.generate("做一个杯子")   # different → fresh generation
    assert orch.code_gen.generate_calls == 2
