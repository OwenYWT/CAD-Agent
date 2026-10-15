import pytest
from app.agent.durable_plan import resolve_solid_count_policy


def plan(count=None, assembly=False):
    checks = [] if count is None else [{'check_id': 'solids', 'kind': 'solid_count', 'required': True,
        'source_kind': 'user', 'comparison': {'op': 'eq', 'value': count, 'unit': 'count'}}]
    return {'operation': 'generate', 'model_kind': 'assembly' if assembly else 'simple',
        'design_brief': {'acceptance': {'schema_version': 'engineering-acceptance.v2', 'checks': checks}},
        'steps': ([{'step_key': 'part', 'kind': 'assembly_part'}, {'step_key': 'final', 'kind': 'assembly_combine'}]
                  if assembly else [{'step_key': 'model', 'kind': 'model'}])}


def test_explicit_two_solids_replaces_default_one_and_unspecified_part_stays_one():
    assert resolve_solid_count_policy(plan(2))['steps']['model']['expected_solid_count'] == 2
    assert resolve_solid_count_policy(plan())['steps']['model'] == {'expected_solid_count': 1, 'source': 'single_part_policy'}


def test_assembly_total_is_not_applied_to_component():
    resolved = resolve_solid_count_policy(plan(2, assembly=True))['steps']
    assert resolved['part']['expected_solid_count'] == 1
    assert resolved['final']['expected_solid_count'] == 2


def test_conflicting_counts_fail_before_execution_and_range_is_not_defaulted():
    value = plan(2)
    value['design_brief']['acceptance']['checks'].append({'check_id': 'other', 'kind': 'solid_count', 'required': True,
        'comparison': {'op': 'eq', 'value': 1, 'unit': 'count'}})
    with pytest.raises(ValueError, match='conflict'):
        resolve_solid_count_policy(value)
    value['design_brief']['acceptance']['checks'] = [{'kind': 'solid_count', 'required': True,
        'comparison': {'op': 'ge', 'lower': 2, 'unit': 'count'}}]
    assert resolve_solid_count_policy(value)['steps']['model']['expected_solid_count'] is None
