"""A proved coordinate qualifier covers only its own source text."""
import copy

import pytest

from app.validation.requirement_binding import (
    RequirementBindingError,
    extract_requirements,
    validate_requirement_bindings,
)
from tests.test_requirement_binding import parsed_contract


def selected_state():
    return {'revision_id': 'base', 'selection_context': {
        'revision_id': 'base', 'parameter_state_sha256': 'a' * 64,
        'features': [{'feature_id': 'confirmed-body', 'kernel_name': 'Body',
                      'type': 'PartDesign::Body'}]}}


@pytest.mark.parametrize('objective,frame,axis', [
    ('将所选对象沿局部Z轴高度改为3mm', 'selected_object', [0, 0, 1]),
    ('把选中的部件沿局部X轴长度改为8mm', 'selected_object', [1, 0, 0]),
    ('沿世界Z轴高度3mm', 'world', [0, 0, 1]),
    ('height 3 mm along world Z axis', 'world', [0, 0, 1]),
])
def test_proven_coordinate_qualifier_is_part_of_requirement_coverage(objective, frame, axis):
    state = selected_state()
    result = extract_requirements(objective, verified_state=state)
    assert not result['unsupported'], result
    assert result['obligations'] and all(o['status'] == 'bound' for o in result['obligations'])
    requirement, = result['requirements']
    assert requirement['scope']['frame'] == frame and requirement['scope']['axis'] == axis
    contract = parsed_contract(objective, verified_state=state)
    assert validate_requirement_bindings(objective, contract, state)['status'] == 'bound'
    changed = contract.checks[0].model_copy(update={'scope': contract.checks[0].scope.model_copy(
        update={'axis': (0., 1., 0.)})})
    with pytest.raises(RequirementBindingError, match='scope'):
        validate_requirement_bindings(objective, contract.model_copy(update={'checks': (changed,)}), state)


@pytest.mark.parametrize('objective', [
    '将所选对象沿未知路径沿局部Z轴高度改为3mm',
    '将所选对象沿局部Z轴高度改为3mm并绕未知轴旋转',
    '沿未知路径沿世界Z轴高度3mm',
    '将所选对象沿局部Z轴与世界Z轴高度改为3mm',
    '所选对象，沿局部Z轴高度3mm',
])
def test_coordinate_coverage_cannot_discharge_unknown_or_unbound_qualifiers(objective):
    assert extract_requirements(objective, verified_state=selected_state())['unsupported']


@pytest.mark.parametrize('invalid', ['missing', 'stale', 'multiple', 'topology'])
def test_local_coordinate_coverage_still_requires_frozen_body_authority(invalid):
    state = copy.deepcopy(selected_state())
    selection = state['selection_context']
    if invalid == 'missing':
        state = None
    elif invalid == 'stale':
        selection['revision_id'] = 'old'
    elif invalid == 'multiple':
        selection['features'].append({**selection['features'][0], 'feature_id': 'another'})
    else:
        selection['topology_selector'] = {'face': 1}
    assert extract_requirements('将所选对象沿局部Z轴高度改为3mm', verified_state=state)['unsupported']
