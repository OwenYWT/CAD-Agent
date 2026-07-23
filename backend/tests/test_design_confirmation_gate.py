import pytest

from app.agent.orchestrator import Orchestrator
from app.models.schemas import CADPlan, DesignBrief


NON_BLOCKING_QUESTION = "\u5939\u5177\u9700\u8981\u9002\u914d\u7684\u684c\u9762\u539a\u5ea6\u662f\u591a\u5c11\uff1f"
BLOCKING_QUESTION = "\u5fc5\u987b\u786e\u8ba4\uff1a\u7f3a\u5c11\u7528\u4e8e\u7cbe\u786e\u914d\u5408\u7684\u5bf9\u8c61\u5c3a\u5bf8\u3002"


class FakePlanner:
    async def plan_new(self, messages):
        return CADPlan(
            description="Adjustable clamp",
            part_type="bracket",
            dimensions={},
            features=["clamp body"],
            design_brief=DesignBrief(
                intent_summary="A printable adjustable clamp",
                artifact_type="clamp",
                open_questions=["What desk thickness should this clamp fit?"],
            ),
        )


class BlockingPlanner:
    async def plan_new(self, messages):
        return CADPlan(
            description="Precision fitted clamp",
            part_type="bracket",
            dimensions={},
            features=["precision fit"],
            design_brief=DesignBrief(
                intent_summary="\u9700\u8981\u7cbe\u786e\u914d\u5408\u7684\u5939\u5177",
                artifact_type="\u5939\u5177",
                open_questions=[BLOCKING_QUESTION],
            ),
        )


class ExplodingCodeGenerator:
    async def generate(self, *args, **kwargs):
        raise AssertionError("code generation should wait for explicit blocking confirmation")


class FakeRetriever:
    async def find_similar(self, *args, **kwargs):
        return []


class RecordingCodeGenerator:
    def __init__(self):
        self.called = False

    async def generate(self, *args, **kwargs):
        self.called = True
        return "result = cq.Workplane('XY').box(10, 10, 10)"


class RecordingPlanner:
    def __init__(self):
        self.messages = None

    async def plan_new(self, messages):
        self.messages = messages
        return CADPlan(
            description="Adjustable clamp",
            part_type="bracket",
            dimensions={},
            features=["clamp body"],
            design_brief=DesignBrief(
                intent_summary="A printable adjustable clamp",
                artifact_type="clamp",
                open_questions=["Confirm screw diameter."],
            ),
        )


async def fake_execute_with_retry(self, request_id, code, plan, output_formats, on_step, prompt, is_2d=False):
    from app.models.schemas import GenerateResponse

    return GenerateResponse(
        request_id=request_id,
        success=True,
        code=code,
        files={"stl": f"/api/files/{request_id}/model.stl"},
        design_brief=plan.design_brief if plan else None,
    )


def build_test_orchestrator(planner, code_gen=None):
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.planner = planner
    orchestrator.code_gen = code_gen or RecordingCodeGenerator()
    orchestrator.retriever = FakeRetriever()
    orchestrator.code_cache = None
    orchestrator._execute_with_retry = fake_execute_with_retry.__get__(orchestrator, Orchestrator)
    return orchestrator


@pytest.mark.asyncio
async def test_generate_continues_when_open_questions_are_non_blocking():
    code_gen = RecordingCodeGenerator()
    orchestrator = build_test_orchestrator(FakePlanner(), code_gen)

    result = await orchestrator.generate("make an adjustable clamp")

    assert result.success is True
    assert result.needs_confirmation is False
    assert code_gen.called is True
    assert result.design_brief is not None
    assert result.design_brief.open_questions == [NON_BLOCKING_QUESTION]


@pytest.mark.asyncio
async def test_generate_blocks_only_for_explicit_must_confirm_questions():
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.planner = BlockingPlanner()
    orchestrator.code_gen = ExplodingCodeGenerator()
    orchestrator.code_cache = None

    result = await orchestrator.generate("make a precision fitted clamp")

    assert result.success is False
    assert result.needs_confirmation is True
    assert result.code is None
    assert result.design_brief is not None
    assert result.design_brief.open_questions == [BLOCKING_QUESTION]
    assert result.plan is not None
    assert result.error["type"] == "NeedsConfirmation"


@pytest.mark.asyncio
async def test_pending_design_brief_context_is_included_when_user_answers_open_question():
    planner = RecordingPlanner()
    orchestrator = build_test_orchestrator(planner)
    context = type("Context", (), {})()
    context.messages = []
    context.current_code = None
    context.current_params = None
    context.generation_count = 0
    context.assembly_parts = None
    context.current_design_brief = DesignBrief(
        intent_summary="A printable adjustable clamp",
        artifact_type="clamp",
        open_questions=["What desk thickness should this clamp fit?"],
    )

    await orchestrator.handle_message(context, "It should fit a 25 mm desk")

    prompt = planner.messages[0]["content"]
    assert "A printable adjustable clamp" in prompt
    assert "What desk thickness should this clamp fit?" in prompt
    assert "It should fit a 25 mm desk" in prompt


@pytest.mark.asyncio
async def test_confirmation_result_carries_recovery_actions_for_blocking_questions():
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.planner = BlockingPlanner()
    orchestrator.code_gen = ExplodingCodeGenerator()
    orchestrator.code_cache = None

    result = await orchestrator.generate("make a precision fitted clamp")

    assert result.recovery_actions
    assert result.recovery_actions[0].action_type == "clarify"
    assert "\u5fc5\u987b\u786e\u8ba4" in result.recovery_actions[0].prompt
