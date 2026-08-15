"""Source-only durable modeling adapter contracts."""
from __future__ import annotations

import pytest

from app.agent.durable_plan import AgentPlan, AgentPlanStep, AffectedObject
from app.models.schemas import CADPlan, DesignBrief
from app.workflows.modeling import DurableModelingSourceGenerator


def _provenance():
    return {
        "provider": "test-provider",
        "model": "controlled-test-model",
        "provider_response_id": "completion-test",
        "request_hash": "a" * 64,
        "response_hash": "b" * 64,
        "finish_reason": "stop",
        "usage": {"total_tokens": 10},
    }


class RetrieverStub:
    def __init__(self):
        self.calls = []

    async def find_similar(self, query, top_k=3, **kwargs):
        self.calls.append((query, top_k, kwargs))
        return [{"description": "example"}]


class CodeGeneratorStub:
    def __init__(self):
        self.generate_calls = 0
        self.step_calls = []

    async def generate(self, plan, examples, conversation, extra_context=""):
        self.generate_calls += 1
        return "import cadquery as cq\nresult = cq.Workplane('XY').box(20,10,4)\n"

    async def generate_step(
        self,
        description,
        accumulated,
        step_index,
        total_steps,
        plan_context="",
    ):
        self.step_calls.append((description, accumulated, step_index, total_steps))
        if step_index == 0:
            return "import cadquery as cq\nresult = cq.Workplane('XY').box(20,10,4)"
        return "result = result.faces('>Z').workplane().hole(3)\nshow_object(result)"

    async def generate_single_part(
        self, part_name, part_description, part_dimensions, examples
    ):
        return (
            "import cadquery as cq\n"
            f"def make_{part_name}():\n"
            "    return cq.Workplane('XY').box(10, 10, 2)\n"
            f"result = make_{part_name}()\n"
        )

    async def generate_assembly_combiner(self, part_codes):
        names = [item["name"] for item in part_codes]
        return (
            "import cadquery as cq\n"
            + "\n".join(
                f"def make_{name}(): return cq.Workplane('XY').box(10, 10, 2)"
                for name in names
            )
            + "\nresult = cq.Assembly()\n"
        )


def _requirements(**overrides):
    values = {
        "description": "20 x 10 x 4 mm 安装支架",
        "part_type": "bracket",
        "dimensions": {"length": 20, "width": 10, "height": 4},
        "features": ["两个安装孔"],
        "constraints": [],
        "modeling_hint": "extrude_cut",
        "design_brief": DesignBrief(
            intent_summary="创建支架",
            artifact_type="bracket",
        ),
    }
    values.update(overrides)
    return CADPlan(**values).model_dump(mode="json")


def _plan(*steps, model_kind="simple"):
    return AgentPlan(
        objective="创建安装支架",
        operation="generate",
        model_kind=model_kind,
        modeling_strategy="extrude_cut",
        design_brief=DesignBrief(
            intent_summary="创建支架",
            artifact_type="bracket",
        ),
        affected_objects=(
            AffectedObject(
                object_id="part-main",
                object_type="part",
                label="支架",
                change="create",
            ),
        ),
        steps=steps,
    )


@pytest.mark.asyncio
async def test_simple_source_uses_retrieval_and_codegen_without_execution():
    retriever = RetrieverStub()
    codegen = CodeGeneratorStub()
    service = DurableModelingSourceGenerator(
        retriever=retriever,
        code_generator=codegen,
        provenance_reader=_provenance,
    )
    step = AgentPlanStep(
        step_key="model-main",
        kind="model",
        description="创建支架",
        affected_object_ids=("part-main",),
        output_formats=("step", "stl"),
    )

    generated = await service.generate_step_source(
        plan=_plan(step),
        step=step,
        requirements=_requirements(),
        previous_source=None,
        step_index=0,
    )

    assert "cq.Workplane" in generated.source_code
    assert generated.mode == "3d"
    assert generated.provenance["provider_response_id"] == "completion-test"
    assert codegen.generate_calls == 1
    assert len(retriever.calls) == 1
    assert not hasattr(service, "backend")
    assert not hasattr(service, "executor")


@pytest.mark.asyncio
async def test_complex_steps_accumulate_source_and_remove_duplicate_imports():
    codegen = CodeGeneratorStub()
    service = DurableModelingSourceGenerator(
        retriever=RetrieverStub(),
        code_generator=codegen,
        provenance_reader=_provenance,
    )
    first = AgentPlanStep(
        step_key="model-01-base",
        kind="model",
        description="创建底板",
        affected_object_ids=("part-main",),
    )
    second = AgentPlanStep(
        step_key="model-02-secondary",
        kind="model",
        description="添加孔",
        depends_on=("model-01-base",),
        affected_object_ids=("part-main",),
        output_formats=("step", "stl"),
    )
    plan = _plan(first, second, model_kind="complex")

    generated1 = await service.generate_step_source(
        plan=plan,
        step=first,
        requirements=_requirements(part_type="custom"),
        previous_source=None,
        step_index=0,
    )
    generated2 = await service.generate_step_source(
        plan=plan,
        step=second,
        requirements=_requirements(part_type="custom"),
        previous_source=generated1.source_code,
        step_index=1,
    )

    source2 = generated2.source_code
    assert source2.count("import cadquery as cq") == 1
    assert "box(20,10,4)" in source2
    assert ".hole(3)" in source2
    assert "show_object" not in source2
    assert [call[2] for call in codegen.step_calls] == [0, 1]


@pytest.mark.asyncio
async def test_assembly_parts_and_combiner_use_exact_persisted_metadata():
    codegen = CodeGeneratorStub()
    service = DurableModelingSourceGenerator(
        retriever=RetrieverStub(),
        code_generator=codegen,
        provenance_reader=_provenance,
    )
    housing = AgentPlanStep(
        step_key="part-01",
        kind="assembly_part",
        description="生成壳体",
        affected_object_ids=("part-01",),
        part_name="housing",
        part_dimensions={"length": 20},
        part_position=(0, 0, 0),
        part_color="lightgray",
    )
    shaft = AgentPlanStep(
        step_key="part-02",
        kind="assembly_part",
        description="生成轴",
        affected_object_ids=("part-02",),
        part_name="shaft",
        part_dimensions={"diameter": 4},
        part_position=(5, 0, 2),
        part_color="steelblue",
    )
    combine = AgentPlanStep(
        step_key="combine",
        kind="assembly_combine",
        description="组合壳体和轴",
        depends_on=("part-01", "part-02"),
        affected_object_ids=("part-01", "part-02"),
        output_formats=("step", "stl"),
    )
    plan = AgentPlan(
        objective="创建装配体",
        operation="generate",
        model_kind="assembly",
        modeling_strategy="assembly_combine",
        design_brief=DesignBrief(
            intent_summary="创建装配体", artifact_type="assembly"
        ),
        affected_objects=(
            AffectedObject(
                object_id="part-01",
                object_type="part",
                label="housing",
                change="create",
            ),
            AffectedObject(
                object_id="part-02",
                object_type="part",
                label="shaft",
                change="create",
            ),
        ),
        steps=(housing, shaft, combine),
    )
    first = await service.generate_step_source(
        plan=plan,
        step=housing,
        requirements={},
        previous_source=None,
        step_index=0,
    )
    second = await service.generate_step_source(
        plan=plan,
        step=shaft,
        requirements={},
        previous_source=None,
        step_index=1,
    )
    combined = await service.generate_step_source(
        plan=plan,
        step=combine,
        requirements={
            "part_sources": [
                {
                    "step_key": "part-01",
                    "function_name": "part_01",
                    "part_name": "housing",
                    "position": [0, 0, 0],
                    "color": "lightgray",
                    "source_code": first.source_code,
                },
                {
                    "step_key": "part-02",
                    "function_name": "part_02",
                    "part_name": "shaft",
                    "position": [5, 0, 2],
                    "color": "steelblue",
                    "source_code": second.source_code,
                },
            ]
        },
        previous_source=None,
        step_index=2,
    )

    assert "make_part_01" in first.source_code
    assert "make_part_02" in second.source_code
    assert combined.generator_kind == "generate_assembly_combiner"
