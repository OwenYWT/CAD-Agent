"""Unit contracts for planning-only durable Agent orchestration."""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.agent.assembly_planner import AssemblyPart, AssemblyPlan
from app.agent.durable_plan import ConfirmationPolicy
from app.agent.durable_planner import DurableAgentPlanner
from app.agent.multi_step import BuildPhase, BuildPlan, BuildStep
from app.models.schemas import CADPlan, DesignBrief, ModificationPlan


class PlannerStub:
    def __init__(self, plan: CADPlan | None = None, error: Exception | None = None):
        self.plan = plan
        self.error = error
        self.new_calls = 0
        self.modify_calls = 0

    async def plan_new(self, messages):
        self.new_calls += 1
        if self.error:
            raise self.error
        return self.plan

    async def plan_modification(self, messages, current_code):
        self.modify_calls += 1
        if self.error:
            raise self.error
        return ModificationPlan(
            description="将宽度改为 30 mm",
            modification_type="dimension_change",
            target_params={"width": 30},
        )


class DecomposerStub:
    def __init__(self, result: BuildPlan):
        self.result = result
        self.calls = 0

    async def decompose(self, plan, *, allow_fallback=True):
        self.calls += 1
        assert allow_fallback is False
        return self.result


class AssemblyPlannerStub:
    def __init__(self, result: AssemblyPlan):
        self.result = result
        self.calls = 0

    async def plan_assembly(self, plan, *, allow_fallback=True):
        self.calls += 1
        assert allow_fallback is False
        return self.result


def _cad_plan(**overrides):
    values = {
        "description": "20 x 10 x 4 mm 安装支架",
        "part_type": "bracket",
        "dimensions": {"length": 20, "width": 10, "height": 4},
        "features": ["两个安装孔"],
        "constraints": [],
        "modeling_hint": "extrude_cut",
        "design_brief": DesignBrief(
            intent_summary="创建安装支架",
            artifact_type="bracket",
        ),
    }
    values.update(overrides)
    return CADPlan(**values)


def _planner(plan):
    decomposer = DecomposerStub(
        BuildPlan(
            steps=[BuildStep(BuildPhase.BASE, "创建基础实体")],
            complexity="simple",
        )
    )
    assembly = AssemblyPlannerStub(
        AssemblyPlan(parts=[], assembly_description="unused")
    )
    return DurableAgentPlanner(
        planner=PlannerStub(plan),
        decomposer=decomposer,
        assembly_planner=assembly,
    ), decomposer, assembly


@pytest.mark.asyncio
async def test_simple_known_part_skips_decomposer_and_source_generation():
    service, decomposer, assembly = _planner(_cad_plan())

    result = await service.plan_generation(
        "创建 20 x 10 x 4 mm 安装支架",
        output_formats=("step", "stl"),
    )

    assert result.model_kind == "simple"
    assert len(result.steps) == 1
    assert result.steps[0].output_formats == ("step", "stl")
    assert decomposer.calls == 0
    assert assembly.calls == 0
    assert not hasattr(service, "code_gen")
    assert not hasattr(service, "retriever")


@pytest.mark.asyncio
async def test_complex_part_maps_ordered_build_plan_without_code():
    plan = _cad_plan(
        part_type="custom",
        features=["壳体", "安装柱", "散热筋"],
    )
    decomposer = DecomposerStub(
        BuildPlan(
            steps=[
                BuildStep(BuildPhase.BASE, "创建基础壳体"),
                BuildStep(BuildPhase.PRIMARY, "抽壳并添加安装柱"),
                BuildStep(BuildPhase.SECONDARY, "添加散热筋"),
            ],
            complexity="complex",
        )
    )
    service = DurableAgentPlanner(
        planner=PlannerStub(plan),
        decomposer=decomposer,
        assembly_planner=AssemblyPlannerStub(
            AssemblyPlan(parts=[], assembly_description="unused")
        ),
    )

    result = await service.plan_generation("创建复杂壳体")

    assert result.model_kind == "complex"
    assert [step.step_key for step in result.steps] == [
        "model-01-base",
        "model-02-primary",
        "model-03-secondary",
    ]
    assert result.steps[1].depends_on == ("model-01-base",)
    assert result.steps[2].depends_on == ("model-02-primary",)
    assert result.steps[-1].output_formats == ("step", "stl")
    assert decomposer.calls == 1


@pytest.mark.asyncio
async def test_assembly_maps_parts_and_final_combine_step():
    plan = _cad_plan(part_type="assembly", features=["壳体", "轴"])
    assembly = AssemblyPlannerStub(
        AssemblyPlan(
            assembly_description="壳体与轴装配",
            parts=[
                AssemblyPart(name="housing", description="生成壳体"),
                AssemblyPart(name="shaft", description="生成轴"),
            ],
        )
    )
    service = DurableAgentPlanner(
        planner=PlannerStub(plan),
        decomposer=DecomposerStub(
            BuildPlan(steps=[], complexity="complex")
        ),
        assembly_planner=assembly,
    )

    result = await service.plan_generation("创建壳体与轴装配")

    assert result.model_kind == "assembly"
    assert [item.object_id for item in result.affected_objects] == [
        "part-01",
        "part-02",
    ]
    assert [step.kind for step in result.steps] == [
        "assembly_part",
        "assembly_part",
        "assembly_combine",
    ]
    assert result.steps[-1].depends_on == ("part-01", "part-02")
    assert result.steps[0].part_name == "housing"
    assert result.steps[0].part_position == (0.0, 0.0, 0.0)
    assert assembly.calls == 1


@pytest.mark.asyncio
async def test_blocking_design_question_requires_confirmation():
    plan = _cad_plan(
        design_brief=DesignBrief(
            intent_summary="创建夹具",
            artifact_type="fixture",
            open_questions=["必须先确认桌面厚度，否则无法安全默认"],
        )
    )
    service, _, _ = _planner(plan)

    result = await service.plan_generation("创建桌面夹具")

    assert result.confirmation_policy is ConfirmationPolicy.REQUIRED
    assert "必须先确认" in result.confirmation_reason


@pytest.mark.asyncio
async def test_provider_failure_propagates_without_fallback_plan():
    error = RuntimeError("provider unavailable")
    service = DurableAgentPlanner(
        planner=PlannerStub(error=error),
        decomposer=DecomposerStub(BuildPlan(steps=[], complexity="simple")),
        assembly_planner=AssemblyPlannerStub(
            AssemblyPlan(parts=[], assembly_description="unused")
        ),
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await service.plan_generation("创建支架")


@pytest.mark.asyncio
async def test_modification_plan_requires_confirmation_and_base_revision():
    service = DurableAgentPlanner(
        planner=PlannerStub(),
        decomposer=DecomposerStub(BuildPlan(steps=[], complexity="simple")),
        assembly_planner=AssemblyPlannerStub(
            AssemblyPlan(parts=[], assembly_description="unused")
        ),
    )
    base = uuid4()

    result = await service.plan_modification(
        "width = 20",
        "将宽度改为 30 mm",
        expected_base_revision_id=base,
    )

    assert result.operation == "modify"
    assert result.expected_base_revision_id == base
    assert result.confirmation_policy is ConfirmationPolicy.REQUIRED
    assert result.steps[0].kind == "modify"
