from app.agent.design_brief import ensure_design_brief
from app.agent.prompts import PLANNER_SYSTEM_PROMPT
from app.models.schemas import CADPlan


def test_design_brief_defaults_are_chinese_user_copy():
    plan = CADPlan(description="做一个桌面支架", part_type="bracket", dimensions={}, features=[])

    brief = ensure_design_brief(plan)

    assert brief.assumptions == ["按原型件用途和公制尺寸进行设计"]
    assert "几何体应封闭无破面" in brief.printability_targets
    assert "代码可成功执行" in brief.acceptance_criteria


def test_planner_prompt_requires_chinese_design_brief_fields():
    forbidden = [
        "Concise engineering interpretation",
        "Short assumptions made",
        "Non-blocking details",
        "design_brief must be concise",
        "Open questions",
    ]

    for snippet in forbidden:
        assert snippet not in PLANNER_SYSTEM_PROMPT

    assert "design_brief 内所有面向用户展示的字段必须使用中文" in PLANNER_SYSTEM_PROMPT
    assert "open_questions 必须使用中文疑问句" in PLANNER_SYSTEM_PROMPT


def test_planner_prompt_closes_plain_cylinder_machine_contract():
    assert 'features 只能是 ["base_cylinder:diameter=<diameter>,height=<height>"]' in (
        PLANNER_SYSTEM_PROMPT
    )
    assert 'constraints 只能是 ["sketch_fully_constrained=true"]' in (
        PLANNER_SYSTEM_PROMPT
    )
