"""Classified durable repair policy and provider adapter tests."""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.agent.durable_repair import DurableRepairSourceGenerator, decide_repair
from app.agent.code_gen import CodeGenerator
from app.config import settings


@pytest.mark.asyncio
@pytest.mark.parametrize("content,finish,error", [
    ("result = outer.cut(cavity)", "stop", None),
    ("result = outer", "length", "truncated"),
    ("", "stop", "empty"),
])
async def test_kernel_and_dfm_repair_require_complete_streamed_code(monkeypatch, content, finish, error):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content), finish_reason=finish,
        )])

    generator = CodeGenerator()
    generator._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    if error:
        with pytest.raises(ValueError, match=f"code repair.*{error}"):
            await generator.fix_error("result = outer", {"type": "CADKernelError", "message": "invalid shell"})
    else:
        assert await generator.fix_error("result = outer", {"type": "CADKernelError", "message": "invalid shell"}) == content
    assert len(calls) == 1
    assert calls[0].get("stream") is True
    assert "max_tokens" not in calls[0]
    assert calls[0]["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_single_part_prompt_requires_attached_features_to_overlap_parent():
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="def make_part_01():\n    return result"),
            finish_reason="stop",
        )])

    generator = CodeGenerator()
    generator._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    await generator.generate_single_part(
        "part_01",
        "矩形外壳盒体，四角有外部安装耳，安装耳必须与盒体连接",
        {"envelope_length": 90.0},
        [],
    )

    prompt = calls[0]["messages"][0]["content"]
    assert "必须与主体实体有实际体积重叠" in prompt
    assert "不得使用正向间隙" in prompt


@pytest.mark.asyncio
async def test_single_part_prompt_prevents_body_derived_double_subtraction():
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="def make_part_01():\n    return result"),
            finish_reason="stop",
        )])

    generator = CodeGenerator()
    generator._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    await generator.generate_single_part(
        "part_01",
        "Open rectangular enclosure with a blind seal groove",
        {"length": 90.0, "width": 60.0, "height": 32.0},
        [],
    )

    prompt = calls[0]["messages"][0]["content"]
    assert "cutBlind() already returns the modified parent body" in prompt
    assert "assign that return value directly" in prompt


@pytest.mark.asyncio
async def test_static_analysis_repair_prompt_explains_double_subtraction():
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="result = repaired"),
            finish_reason="stop",
        )])

    generator = CodeGenerator()
    generator._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    await generator.fix_error(
        "result = result - groove_cut",
        {
            "type": "StaticAnalysis",
            "message": (
                "A body-derived cutBlind result already applies the subtraction "
                "to its parent body"
            ),
        },
    )

    prompt = calls[0]["messages"][0]["content"]
    assert "cutBlind() already returns the modified parent body" in prompt
    assert "Never subtract that returned body from its parent again" in prompt


@pytest.mark.asyncio
async def test_geometry_repair_prompt_explains_disconnected_solid_count_failure():
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="result = repaired"),
            finish_reason="stop",
        )])

    generator = CodeGenerator()
    generator._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    await generator.fix_error(
        "result = source",
        {
            "type": "GeometryError",
            "message": "artifact-00:solid_count_mismatch; expected 2, got 5",
        },
    )

    prompt = calls[0]["messages"][0]["content"]
    assert "solid_count_mismatch" in prompt
    assert "不得使用正向间隙" in prompt
    assert "保持上盖作为独立装配件" in prompt


@pytest.mark.asyncio
async def test_repair_prompt_handles_non_coplanar_face_selection():
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="result = repaired"),
            finish_reason="stop",
        )])

    generator = CodeGenerator()
    generator._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    await generator.fix_error(
        "result = body.faces('\\|Y').workplane().hole(4)",
        {
            "type": "UserCodeError",
            "message": "Selected faces must be co-planar.",
        },
    )

    prompt = calls[0]["messages"][0]["content"]
    assert "Selected faces must be co-planar" in prompt
    assert "single co-planar face" in prompt
    assert "do not use .faces(\"|Y\")" in prompt
    assert "Workplane.cleanup()" in prompt


@pytest.mark.asyncio
async def test_single_part_prompt_prevents_non_coplanar_workplanes():
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="def make_part_01():\n    return result"),
            finish_reason="stop",
        )])

    generator = CodeGenerator()
    generator._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    await generator.generate_single_part("part_01", "box", {"length": 10.0}, [])

    prompt = calls[0]["messages"][0]["content"]
    assert "single co-planar face" in prompt
    assert "do not use .faces(\"|Y\")" in prompt
    assert "Workplane.cleanup()" in prompt


@pytest.mark.asyncio
async def test_rectangular_assembly_repair_prompt_preserves_axis_aligned_tabs():
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="result = repaired"),
            finish_reason="stop",
        )])

    generator = CodeGenerator()
    generator._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    await generator.fix_error(
        "result = assembly",
        {
            "type": "GeometryError",
            "message": (
                "Geometry validation failed: unclassified oblique planar boundary "
                "in rectangular assembly"
            ),
        },
    )

    prompt = calls[0]["messages"][0]["content"]
    assert "axis-aligned" in prompt
    assert "45" in prompt
    assert "rectangular assembly" in prompt


@pytest.mark.asyncio
async def test_single_part_prompt_keeps_rectangular_tabs_axis_aligned():
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="def make_part_01():\n    return result"),
            finish_reason="stop",
        )])

    generator = CodeGenerator()
    generator._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    await generator.generate_single_part("part_01", "rectangular enclosure with tabs", {}, [])

    prompt = calls[0]["messages"][0]["content"]
    assert "axis-aligned" in prompt
    assert "45" in prompt


@pytest.mark.parametrize(
    "category,error_code",
    [
        ("infrastructure", "runtime_unavailable"),
        ("timeout", "execution_timeout"),
        ("resource", "execution_oom"),
        ("artifact", "artifact_rejected"),
        ("cancellation", "execution_cancelled"),
        ("provider", "provider_unavailable"),
        ("lease", "stale_lease"),
        ("database", "database_unavailable"),
        ("object_store", "object_store_unavailable"),
    ],
)
def test_non_code_failures_are_hard_stops(category, error_code):
    decision = decide_repair(
        category=category,
        error_code=error_code,
        error_message="controlled failure",
        runtime_error_type=None,
        repair_count=0,
        seen_signatures=(),
    )
    assert decision.repairable is False
    assert decision.strategy == "hard_stop"


def test_user_code_and_kernel_failures_are_bounded_and_repeat_stops():
    first = decide_repair(
        category="user_code",
        error_code="user_code_failed",
        error_message="SyntaxError on line 3",
        runtime_error_type="SyntaxError",
        repair_count=0,
        seen_signatures=(),
    )
    repeated = decide_repair(
        category="user_code",
        error_code="user_code_failed",
        error_message="SyntaxError on line 3",
        runtime_error_type="SyntaxError",
        repair_count=1,
        seen_signatures=(first.signature,),
    )
    kernel = decide_repair(
        category="cad_kernel",
        error_code="cad_execution_failed",
        error_message="BRep_API: not done",
        runtime_error_type="CADKernelError",
        repair_count=1,
        seen_signatures=(),
    )
    assert first.repairable is True
    assert first.budget == 2
    assert repeated.repairable is False
    assert repeated.strategy == "repeated_error_stop"
    assert kernel.repairable is True
    assert kernel.strategy == "remove_or_reduce_refinement"


def test_failure_signature_ignores_attempt_ids_hashes_and_line_numbers():
    first = decide_repair(
        category="user_code",
        error_code="user_code_failed",
        error_message=(
            "SyntaxError line 3 attempt "
            "6b0deee9-e5eb-4d10-b57b-c12ac076c9a4 "
            + "a" * 64
        ),
        runtime_error_type="SyntaxError",
        repair_count=0,
        seen_signatures=(),
    )
    repeated = decide_repair(
        category="user_code",
        error_code="user_code_failed",
        error_message=(
            "SyntaxError line 7 attempt "
            "97971586-62b4-45c1-bbb4-50cdcfaf5ec8 "
            + "b" * 64
        ),
        runtime_error_type="SyntaxError",
        repair_count=1,
        seen_signatures=(first.signature,),
    )
    assert repeated.repairable is False
    assert repeated.strategy == "repeated_error_stop"


def test_geometry_failure_uses_targeted_bounded_source_repair():
    decision = decide_repair(
        category="validation",
        error_code="geometry_validation_failed",
        error_message="stl:not_watertight; stl:dimension_mismatch",
        runtime_error_type="GeometryError",
        repair_count=0,
        seen_signatures=(),
    )
    assert decision.repairable is True
    assert decision.failure_class == "geometry_invalid"
    assert decision.strategy == "minimal_targeted_fix"
    assert decision.budget == 2


def test_visual_failure_uses_bounded_visual_repair_path():
    decision = decide_repair(
        category="validation",
        error_code="visual_validation_failed",
        error_message="Visual validation issues: centered hole appears offset",
        runtime_error_type="VisualError",
        repair_count=0,
        seen_signatures=(),
    )
    assert decision.repairable is True
    assert decision.failure_class == "vision_mismatch"
    assert decision.strategy == "minimal_targeted_fix"
    assert decision.budget == 2


class CodeGeneratorStub:
    calls = 0

    async def fix_error(self, source, error):
        self.calls += 1
        return source.replace("box(", "box(20, ")


@pytest.mark.asyncio
async def test_repair_adapter_records_provenance_and_rejects_unchanged_source():
    codegen = CodeGeneratorStub()
    service = DurableRepairSourceGenerator(
        code_generator=codegen,
        provenance_reader=lambda: {
            "provider": "controlled-provider",
            "model": "controlled-model",
            "provider_response_id": "repair-1",
            "request_hash": "a" * 64,
            "response_hash": "b" * 64,
            "finish_reason": "stop",
            "usage": {},
        },
    )
    decision = decide_repair(
        category="user_code",
        error_code="user_code_failed",
        error_message="TypeError",
        runtime_error_type="TypeError",
        repair_count=0,
        seen_signatures=(),
    )
    result = await service.repair(
        source_code="result = box(10, 4)",
        failure={
            "category": "user_code",
            "error_code": "user_code_failed",
            "error_message": "TypeError",
            "runtime_error_type": "TypeError",
        },
        decision=decision,
    )
    assert result.source_code != "result = box(10, 4)"
    assert result.provenance["provider_response_id"] == "repair-1"
    assert codegen.calls == 1
