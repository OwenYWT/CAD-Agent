import pytest
from pydantic import ValidationError

from app.freecad.contracts import SketchAddConstraintArgs, capability_schemas
from app.freecad.operation_generator import _SYSTEM_PROMPT


def test_prompt_defers_constraint_kinds_and_references_to_actual_schema():
    assert 'second is allowed ONLY' not in _SYSTEM_PROMPT
    assert 'kinds are horizontal,' not in _SYSTEM_PROMPT
    examples = capability_schemas()['sketch.add_constraint']['examples']
    assert any(e['kind'] == 'point_on_object' for e in examples)
    for example in examples:
        SketchAddConstraintArgs.model_validate(example)


@pytest.mark.parametrize('second', [{'datum': 'origin'}, {'geometry_index': 1}, {'datum': 'horizontal_axis', 'point_position': 1}])
def test_point_on_object_does_not_allow_arbitrary_second_reference(second):
    with pytest.raises(ValidationError):
        SketchAddConstraintArgs.model_validate({'sketch': 'Sketch', 'kind': 'point_on_object',
            'first': {'geometry_index': 0, 'point_position': 1}, 'second': second})
