import json
import pytest
from app.agent.multi_step import parse_build_plan, BuildPhase


@pytest.mark.parametrize("content,reason", [(None,"stop"),("","length"),("not json","stop"),('{"steps":[]}',"stop")])
def test_incomplete_decomposition_cannot_become_a_success(content, reason):
    with pytest.raises(ValueError):
        parse_build_plan(content, reason)


def test_valid_complete_response_and_fenced_json():
    content = json.dumps({"complexity":"moderate", "steps":[{"phase":"base","description":"Pad the plate"},{"phase":"primary","description":"Cut the hole"}]})
    result = parse_build_plan("```json\n"+content+"\n```", "stop")
    assert [s.phase for s in result.steps] == [BuildPhase.BASE, BuildPhase.PRIMARY]
    with pytest.raises(ValueError):
        parse_build_plan(content.replace('"primary"', '"invented"'), "stop")


def test_decomposition_limit_matches_the_ten_step_prompt_contract():
    content = {"complexity": "complex", "steps": [{"phase": "secondary", "description": f"Feature {i}"} for i in range(10)]}
    assert len(parse_build_plan(json.dumps(content), "stop").steps) == 10
    content["steps"].append({"phase": "refinement", "description": "One too many"})
    with pytest.raises(ValueError):
        parse_build_plan(json.dumps(content), "stop")
