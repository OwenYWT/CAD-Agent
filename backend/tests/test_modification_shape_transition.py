import pytest

from app.contracts.acceptance_v2 import AcceptanceContractV2
from app.validation.requirement_binding import (
    extract_source_requirements,
    validate_requirement_bindings,
)


OBJECTIVE = "将正方体的宽改为 20cm，使其变成长方体"


def test_cube_to_rectangular_prism_modification_binds_final_width_only():
    extracted = extract_source_requirements(OBJECTIVE)

    assert not extracted["unsupported"], extracted
    assert [row["kind"] for row in extracted["requirements"]] == ["overall_dimension"]
    width = extracted["requirements"][0]
    assert width["property"] == "width"
    assert width["comparison"] == {"op": "eq", "value": 200.0, "unit": "mm"}
    assert width["scope"] == {"axis": [0, 1, 0]}


def test_cube_to_rectangular_prism_acceptance_with_width_check_is_bound():
    extracted = extract_source_requirements(OBJECTIVE)
    width = extracted["requirements"][0]
    contract = AcceptanceContractV2.model_validate({
        "objective": OBJECTIVE,
        "checks": [{
            "check_id": width["requirement_id"],
            "binding_ref": width["requirement_id"],
            "kind": width["kind"],
            "description": width["property"],
            "source_quote": width["source_quote"],
            "comparison": width["comparison"],
            "scope": width["scope"],
        }],
    })

    assert validate_requirement_bindings(OBJECTIVE, contract)["status"] == "bound"


def test_plain_cube_requirement_still_requires_complete_cube_shape():
    extracted = extract_source_requirements("生成一个边长 10cm 的正方体")

    assert not extracted["unsupported"], extracted
    assert any(row["kind"] == "cube_shape" for row in extracted["requirements"])


def test_planner_retry_feedback_does_not_encode_shape_transition_as_cube_zero():
    from app.agent.planner import _correction_messages

    feedback = _correction_messages(
        "{}",
        ValueError("material shape requires its complete primitive boundary (1)"),
    )[-1]["content"]

    assert "shape transition" in feedback
    assert "cube_shape=0" in feedback
    assert "overall_dimension" in feedback


def test_v2_planner_prompt_marks_cube_as_before_state_for_shape_transition():
    from app.agent.planner import _v2_requirements_prompt

    prompt = _v2_requirements_prompt(OBJECTIVE, "submit_requirements")

    assert "before-state" in prompt
    assert "Do not submit cube_shape" in prompt


def test_modification_prompt_requires_semantic_dimension_parameter_names():
    from app.agent.planner import MODIFICATION_SYSTEM_PROMPT

    assert "target_params" in MODIFICATION_SYSTEM_PROMPT
    assert "semantic" in MODIFICATION_SYSTEM_PROMPT
    assert "Pad.Length" in MODIFICATION_SYSTEM_PROMPT


def test_planner_retry_feedback_explains_semantic_dimension_parameter_error():
    from app.agent.planner import _correction_messages

    feedback = _correction_messages(
        "{}",
        ValueError(
            "target_params.width: changed source dimension must use the semantic 'width' parameter"
        ),
    )[-1]["content"]

    assert "target_params.width" in feedback
    assert "exactly the semantic key 'width'" in feedback
    assert "Pad.Length" in feedback


def test_modification_payload_collapses_equivalent_width_aliases_to_semantic_key():
    from app.agent.planner import Planner

    plan = Planner._parse_modification_payload({
        "description": "change width",
        "modification_type": "dimension_change",
        "target_params": {"width": 200, "width_mm": 200},
        "new_features": [],
    })

    assert plan.target_params == {"width": 200.0}


def test_dimension_validation_prefers_canonical_width_when_aliases_are_present():
    from app.models.schemas import ModificationPlan
    from app.validation.acceptance_preflight import validate_modification_dimension_targets

    plan = ModificationPlan(
        description="change width",
        modification_type="dimension_change",
        target_params={"width": 200, "width_mm": 200},
        new_features=[],
    )

    validate_modification_dimension_targets(
        plan,
        "将正方体的宽改为 20cm，使其变成长方体",
    )


def test_shape_transition_drops_before_state_primitive_checks_from_acceptance():
    from app.agent.planner import _normalize_shape_transition_acceptance

    payload = {
        "acceptance": {
            "checks": [
                {"check_id": "width", "kind": "overall_dimension"},
                {"check_id": "cube", "kind": "cube_shape"},
                {"check_id": "edge", "kind": "cube_edge"},
            ]
        }
    }

    _normalize_shape_transition_acceptance(
        payload,
        "将正方体的宽改为 20cm，使其变成长方体",
    )

    assert [check["kind"] for check in payload["acceptance"]["checks"]] == [
        "overall_dimension"
    ]
