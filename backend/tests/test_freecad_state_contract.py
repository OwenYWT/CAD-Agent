from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.freecad.state_contract import (
    FreeCADStateV2,
    ParameterStateError,
    compile_parameter_operation_plan,
    project_state_parameters,
)
from app.services.event_relay import _parameter_change_evidence


def _state():
    return {
        "schema_version": "freecad-state.v2",
        "document": "Model",
        "object_count": 1,
        "root_objects": ["Pad"],
        "objects": [],
        "parameters": [{
            "id": "Pad.Length",
            "object_name": "Pad",
            "property_name": "Length",
            "label": "Pad · Length",
            "group": "Data",
            "property_type": "App::PropertyLength",
            "value": 10.0,
            "unit": "mm",
            "editable": True,
            "minimum": None,
            "maximum": None,
            "step": None,
        }],
    }


def test_state_v2_projects_stable_freecad_parameter_identity():
    state = FreeCADStateV2.model_validate(_state()).model_dump(mode="json")
    parameter = project_state_parameters(state)[0]
    assert parameter.name == "Pad.Length"
    assert parameter.source == "freecad"
    assert parameter.object_name == "Pad"
    assert parameter.line is None


def test_change_set_parameter_diff_uses_stable_freecad_state_identity():
    base = project_state_parameters(_state())
    candidate_state = _state()
    candidate_state["parameters"][0]["value"] = 35.0
    candidate = project_state_parameters(candidate_state)

    assert _parameter_change_evidence(base, candidate) == [{
        "parameter_id": "Pad.Length",
        "label": "Pad · Length",
        "before": 10.0,
        "after": 35.0,
        "unit": "mm",
    }]


def test_change_set_parameter_diff_does_not_compare_incompatible_units():
    base = project_state_parameters(_state())
    candidate = [base[0].model_copy(update={"value": 35.0, "unit": "deg"})]

    assert _parameter_change_evidence(base, candidate) == []


def test_state_v2_rejects_property_unit_mismatch_and_duplicate_ids():
    state = _state()
    state["parameters"][0]["unit"] = "deg"
    with pytest.raises(ValidationError):
        FreeCADStateV2.model_validate(state)
    state = _state()
    state["parameters"].append(dict(state["parameters"][0]))
    with pytest.raises(ValidationError):
        FreeCADStateV2.model_validate(state)


def test_structured_parameter_compiler_is_sorted_typed_and_llm_free():
    plan = compile_parameter_operation_plan(
        _state(),
        {"parameter_updates": [{"parameter_id": "Pad.Length", "value": 12}]},
        output_formats=("step", "stl"),
    )
    operation = plan.operations[0]
    assert operation.action == "property.set"
    assert operation.args == {
        "object": "Pad",
        "property": "Length",
        "value": 12.0,
        "expected_property_type": "App::PropertyLength",
        "unit": "mm",
    }
    assert plan.operations[-1].args["formats"] == ["fcstd", "step", "stl"]


def test_structured_parameter_compiler_fails_closed_for_unknown_parameter():
    with pytest.raises(ParameterStateError) as captured:
        compile_parameter_operation_plan(
            _state(),
            {"parameter_updates": [{"parameter_id": "Pad.Width", "value": 12}]},
            output_formats=("step",),
        )
    assert captured.value.code == "parameter_not_editable"
