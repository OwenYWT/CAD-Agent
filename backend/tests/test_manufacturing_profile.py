import pytest

from app.agent.orchestrator import Orchestrator
from app.models.schemas import CADPlan, DesignBrief, GenerateResponse, ManufacturingProfile


def test_manufacturing_profile_schema_serializes_defaults():
    profile = ManufacturingProfile.fdm_pla_default()

    payload = profile.model_dump()

    assert payload["process"] == "fdm"
    assert payload["material"] == "PLA"
    assert payload["nozzle_diameter_mm"] == 0.4
    assert payload["layer_height_mm"] == 0.2
    assert payload["build_volume_mm"] == [220, 220, 250]


def test_generation_response_carries_manufacturing_profile():
    profile = ManufacturingProfile(process="sla", material="Resin")

    response = GenerateResponse(request_id="req-profile", success=True, manufacturing_profile=profile)

    assert response.model_dump()["manufacturing_profile"]["process"] == "sla"
    assert response.model_dump()["manufacturing_profile"]["material"] == "Resin"


class RecordingPlanner:
    def __init__(self):
        self.messages = None

    async def plan_new(self, messages):
        self.messages = messages
        return CADPlan(
            description="Shelf bracket",
            part_type="bracket",
            dimensions={"wall_thickness": 3},
            features=["two holes"],
            design_brief=DesignBrief(
                intent_summary="Printable shelf bracket",
                artifact_type="bracket",
                open_questions=["Confirm load."],
            ),
        )


class FakeRetriever:
    async def find_similar(self, *args, **kwargs):
        return []


class RecordingCodeGenerator:
    async def generate(self, *args, **kwargs):
        return "result = cq.Workplane('XY').box(10, 10, 10)"


async def fake_execute_with_retry(self, request_id, code, plan, output_formats, on_step, prompt, is_2d=False):
    return GenerateResponse(
        request_id=request_id,
        success=True,
        code=code,
        files={"stl": f"/api/files/{request_id}/model.stl"},
        design_brief=plan.design_brief if plan else None,
    )


def build_test_orchestrator(planner: RecordingPlanner) -> Orchestrator:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.planner = planner
    orchestrator.code_gen = RecordingCodeGenerator()
    orchestrator.retriever = FakeRetriever()
    orchestrator.code_cache = None
    orchestrator._execute_with_retry = fake_execute_with_retry.__get__(orchestrator, Orchestrator)
    return orchestrator


@pytest.mark.asyncio
async def test_generate_injects_manufacturing_profile_before_planning():
    planner = RecordingPlanner()
    orchestrator = build_test_orchestrator(planner)
    profile = ManufacturingProfile(
        process="fdm",
        material="PETG",
        nozzle_diameter_mm=0.4,
        layer_height_mm=0.2,
        build_volume_mm=[220, 220, 250],
    )

    result = await orchestrator.generate("make a shelf bracket", manufacturing_profile=profile)

    prompt = planner.messages[0]["content"]
    assert "Manufacturing profile" in prompt
    assert "process=fdm" in prompt
    assert "material=PETG" in prompt
    assert result.success is True
    assert result.needs_confirmation is False
    assert result.manufacturing_profile == profile
    assert result.plan.manufacturing_profile == profile
    assert "FDM PETG" in result.design_brief.manufacturing_posture


@pytest.mark.asyncio
async def test_generation_brief_shows_manufacturing_profile_in_chinese():
    planner = RecordingPlanner()
    orchestrator = build_test_orchestrator(planner)
    profile = ManufacturingProfile(
        process="fdm",
        material="PETG",
        nozzle_diameter_mm=0.4,
        layer_height_mm=0.2,
        build_volume_mm=[220, 220, 250],
    )

    result = await orchestrator.generate("make a shelf bracket", manufacturing_profile=profile)

    targets = result.design_brief.printability_targets
    assert "\u5236\u9020\u5de5\u827a\uff1aFDM" in targets
    assert "\u6750\u6599\uff1aPETG" in targets
    assert "\u55b7\u5634\u76f4\u5f84\uff1a0.4 mm" in targets
    assert "\u5c42\u9ad8\uff1a0.2 mm" in targets
    assert "\u6210\u578b\u7a7a\u95f4\uff1a220 x 220 x 250 mm" in targets
    assert all("Manufacturing process:" not in item for item in targets)
    assert all("Nozzle diameter:" not in item for item in targets)
