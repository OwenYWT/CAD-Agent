from types import SimpleNamespace

import pytest

from app.models.schemas import CADPlan, DesignBrief
from app.workflows.source_preparation import SourcePreparer
from app.workflows.temporal import McadSourcePreparationRequest


class FakePlanner:
    def __init__(self, plan):
        self.plan = plan
        self.modification_calls = []

    async def plan_new(self, messages):
        return self.plan

    async def plan_modification(self, messages, code):
        self.modification_calls.append((messages, code))
        return SimpleNamespace(
            description="加厚底板",
            model_dump=lambda mode=None: {
                "description": "加厚底板",
                "modification_type": "dimension_change",
                "target_params": {"height": 6},
                "new_features": [],
            },
        )


class FakeRetriever:
    async def find_similar(self, *args, **kwargs):
        return []


class FakeCodeGenerator:
    async def generate(self, plan, examples, conversation, extra_context=""):
        return "import cadquery as cq\nresult = cq.Workplane('XY').box(20, 10, 4)\n"

    async def generate_2d(self, plan, examples, conversation):
        return "import ezdxf\nresult = ezdxf.new()\nresult.saveas('/output/result.dxf')\n"

    async def generate_assembly(self, plan, examples, conversation):
        return "import cadquery as cq\nresult = cq.Assembly()\n"

    async def modify(self, plan, code, examples, conversation):
        return code.replace("box(20, 10, 4)", "box(20, 10, 6)")


class FakeOrchestrator:
    def __init__(self, plan):
        self.planner = FakePlanner(plan)
        self.retriever = FakeRetriever()
        self.code_gen = FakeCodeGenerator()

    def _prompt_with_manufacturing_profile(self, prompt, profile):
        return prompt

    def _apply_manufacturing_profile_to_brief(self, brief, profile):
        return None

    def _lookup_standard_parts(self, plan):
        return ""


def _plan(*, part_type="box", open_questions=None):
    return CADPlan(
        description="参数化测试零件",
        part_type=part_type,
        dimensions={"length": 20, "width": 10, "height": 4},
        features=[],
        design_brief=DesignBrief(
            intent_summary="创建测试零件",
            open_questions=open_questions or [],
        ),
    )


@pytest.mark.asyncio
async def test_generate_preparation_returns_real_execution_source_without_running_cad():
    prepared = await SourcePreparer(FakeOrchestrator(_plan())).prepare(
        McadSourcePreparationRequest(
            operation="generate",
            prompt="创建 20 x 10 x 4 mm 长方体",
        )
    )
    assert prepared["needs_confirmation"] is False
    assert prepared["execution"]["operation"] == "generate"
    assert prepared["execution"]["mode"] == "3d"
    assert "box(20, 10, 4)" in prepared["source_code"]
    assert [item["name"] for item in prepared["execution"]["outputs"]] == [
        "step",
        "stl",
    ]


@pytest.mark.asyncio
async def test_generate_preparation_preserves_design_clarification_gate():
    prepared = await SourcePreparer(
        FakeOrchestrator(
            _plan(open_questions=["必须确认孔径，无法安全默认。"])
        )
    ).prepare(
        McadSourcePreparationRequest(
            operation="generate",
            prompt="创建一个带孔支架",
        )
    )
    assert prepared["needs_confirmation"] is True
    assert prepared["design_brief"]["open_questions"] == [
        "必须确认孔径，无法安全默认。"
    ]
    assert "execution" not in prepared


@pytest.mark.asyncio
async def test_dxf_only_contract_forces_real_2d_generation():
    prepared = await SourcePreparer(FakeOrchestrator(_plan())).prepare(
        McadSourcePreparationRequest(
            operation="generate",
            prompt="创建 100 x 50 mm 安装板图纸",
            output_formats=("dxf",),
        )
    )
    assert prepared["execution"]["mode"] == "2d"
    assert prepared["plan"]["part_type"] == "profile_2d"
    assert prepared["output_formats"] == ["dxf"]
    assert "ezdxf" in prepared["source_code"]


@pytest.mark.asyncio
async def test_modify_preparation_uses_existing_code_and_returns_modified_source():
    prepared = await SourcePreparer(FakeOrchestrator(_plan())).prepare(
        McadSourcePreparationRequest(
            operation="modify",
            prompt="将高度改为 6 mm",
            existing_code=(
                "import cadquery as cq\n"
                "result = cq.Workplane('XY').box(20, 10, 4)\n"
            ),
        )
    )
    assert prepared["execution"]["operation"] == "modify"
    assert "box(20, 10, 6)" in prepared["source_code"]
