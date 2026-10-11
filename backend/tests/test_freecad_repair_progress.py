import json

import pytest

from app.workflows.native_repair import NativeRepairBudget, RepairStopped


POLICY = {'max_attempts': 8, 'cut_attempts': 2, 'constraint_attempts': 6, 'profile_attempts': 2}


def failure(operation, completed=(), signature=None, family='cut', target=None):
    return {'operation_id': operation, 'error_code': 'subtractive_feature_no_effect',
        'error_message': 'feature.pocket did not produce a smaller valid solid',
        'details': {'repair_progress_json': json.dumps({
            'failed_operation_id': operation, 'completed_operations': list(completed),
            'target': target or operation, 'family': family, 'signature': signature or operation})}}


def test_distinct_cuts_can_progress_past_two_without_discarding_global_limit():
    budget = NativeRepairBudget(POLICY)
    for index in range(4):
        assert budget.reserve(failure(str(index), map(str, range(index)))) == 1
    assert budget.used == 4
    assert budget.resolved_operations == ['0', '1', '2']


def test_unchanged_physical_failure_stops_before_another_provider_call():
    budget = NativeRepairBudget(POLICY)
    budget.reserve(failure('cut', signature='same-geometry'))
    with pytest.raises(RepairStopped, match='没有几何或求解进展') as stopped:
        budget.reserve(failure('cut', signature='same-geometry'))
    assert stopped.value.code == 'agent_freecad_repair_no_progress'
    assert budget.used == 1


def test_regression_to_an_earlier_operation_is_not_forward_progress():
    budget = NativeRepairBudget(POLICY)
    budget.reserve(failure('later-cut', ['pad', 'earlier-cut']))
    with pytest.raises(RepairStopped, match='回退'):
        budget.reserve(failure('earlier-cut', ['pad']))


def test_total_budget_reports_new_failure_and_already_resolved_operations():
    budget = NativeRepairBudget({**POLICY, 'max_attempts': 2})
    budget.reserve(failure('throat'))
    budget.reserve(failure('clamp', ['throat']))
    with pytest.raises(RepairStopped) as stopped:
        budget.reserve(failure('screw', ['throat', 'clamp']))
    assert stopped.value.code == 'agent_freecad_repair_budget_exhausted'
    assert all(value in str(stopped.value) for value in ('screw', 'throat', 'clamp', '2/2', 'subtractive_feature_no_effect'))


def test_per_target_and_cross_checkpoint_budgets_are_both_bounded():
    budget = NativeRepairBudget(POLICY)
    assert budget.reserve(failure('cut', signature='first')) == 1
    assert budget.reserve(failure('cut', signature='moved')) == 2
    with pytest.raises(RepairStopped, match='对象修复预算'):
        budget.reserve(failure('cut', signature='third'))
    budget.succeeded()
    assert budget.reserve(failure('next-checkpoint-cut')) == 1
    assert budget.used == 3 and budget.resolved_operations == ['cut']


def test_constraint_attempts_do_not_count_prior_unrelated_cuts():
    budget = NativeRepairBudget(POLICY)
    budget.reserve(failure('cut1'))
    budget.reserve(failure('cut2', ['cut1']))
    assert budget.reserve(failure('tip', ['cut1', 'cut2'], family='constraint', target='Tip')) == 1
    assert budget.reserve(failure('tip', ['cut1', 'cut2'], signature='one-dof', family='constraint', target='Tip')) == 2


def test_removed_constraint_requires_its_preserved_consumer_before_advancing_to_another_sketch():
    budget = NativeRepairBudget(POLICY)
    first = failure('redundant', family='constraint', target='Clamp')
    observed = json.loads(first['details']['repair_progress_json'])
    observed['progress_operation_id'] = 'pad-clamp'
    first['details']['repair_progress_json'] = json.dumps(observed)
    budget.reserve(first)
    # The deleted constraint need not reappear; the actual Pad must have run.
    assert budget.reserve(failure('hook-relation', ['pad-clamp'], family='constraint', target='Hook')) == 1
    assert budget.resolved_operations == ['redundant']
    blocked = NativeRepairBudget(POLICY)
    blocked.reserve(first)
    with pytest.raises(RepairStopped, match='回退'):
        blocked.reserve(failure('hook-relation', [], family='constraint', target='Hook'))
    # Reaching that feature can expose a different, real failure (e.g. an
    # invalid contour). This is neither a backwards step nor a passed feature.
    assert blocked.reserve(failure('pad-clamp', [], family='profile', target='Clamp')) == 1
    assert not blocked.resolved_operations


def test_changed_solver_failure_within_same_sketch_shares_its_target_budget():
    budget = NativeRepairBudget(POLICY)
    budget.reserve(failure('constraint-first', family='constraint', target='Sketch'))
    assert budget.reserve(failure('constraint-second', family='constraint', target='Sketch', signature='fewer-dofs')) == 2
    assert not budget.resolved_operations


def test_missing_or_mismatched_native_observation_does_not_buy_a_repair():
    budget = NativeRepairBudget(POLICY)
    missing = failure('cut'); missing['details'] = {}
    with pytest.raises(RepairStopped):
        budget.reserve(missing)
    mismatch = failure('cut'); mismatch['operation_id'] = 'other'
    with pytest.raises(RepairStopped):
        budget.reserve(mismatch)
    assert budget.used == 0


def test_constraint_renaming_reordering_and_solver_numbering_are_not_physical_progress():
    from copy import deepcopy
    from app.workflows.native_repair import observation
    snapshot = {'failure': {'code': 'sketch_under_constrained', 'details': {'object': 'Sketch'}},
        'failed_operation_id': 'cut', 'operations': [], 'sketches': [{'name': 'Sketch',
            'geometry': [{'index': 0, 'start': [1.00000000001, 2, 0]}], 'placement': [1, 0, 0],
            'constraints': [{'type': 'Distance', 'value': 3, 'first': 0, 'name': 'old', 'index': 0},
                            {'type': 'Horizontal', 'first': 0, 'index': 1}],
            'solver': {'constraint_status': 'under_constrained', 'degrees_of_freedom': 2,
                       'redundant_constraint_numbers': [1]}}]}
    changed = deepcopy(snapshot)
    sketch = changed['sketches'][0]
    sketch['constraints'].reverse()
    sketch['constraints'][1].update(index=1, name='new')
    sketch['solver']['redundant_constraint_numbers'] = [2]
    sketch['geometry'][0]['start'][0] = 1.00000000002
    sketch['geometry'][0]['start'][2] = -1e-12
    assert observation(snapshot)['signature'] == observation(changed)['signature']
    sketch['solver']['degrees_of_freedom'] = 1
    assert observation(snapshot)['signature'] != observation(changed)['signature']
