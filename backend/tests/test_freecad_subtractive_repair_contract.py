"""Recovery must retain every feature and dimension, not just get an exit code."""
import copy
import json
import pytest
from app.agent.durable_repair import decide_repair
from app.freecad.contracts import FreeCADOperationPlan


def plan():
    return {'schema_version': 'freecad-operation-plan.v1', 'document_name': 'Model', 'operations': [
        {'op_id': 'cut', 'action': 'feature.pocket', 'args': {'name': 'Cavity', 'profile': 'Profile', 'length_mm': 38, 'reversed': True}},
        {'op_id': 'export', 'action': 'document.export', 'args': {'formats': ['fcstd', 'step']}}]}


def test_source_no_effect_is_classified_for_targeted_repair():
    decision = decide_repair(category='validation', error_code='subtractive_feature_no_effect',
        error_message='feature.pocket did not produce a smaller valid solid', runtime_error_type='StructuredExecutionError',
        repair_count=0, seen_signatures=(), operation_id='cut')
    assert decision.repairable
    assert decision.failure_class == 'subtractive_feature_no_effect'


@pytest.mark.parametrize('reason', ['profile_outside_material', 'insufficient_reach'])
def test_measured_no_intersection_rejects_direction_only_without_spending_a_cad_attempt(reason):
    from app.freecad.constraint_repair import compile_cut_patch
    from app.freecad.failure_snapshot import digest
    before = FreeCADOperationPlan.model_validate(plan())
    with pytest.raises(ValueError, match='direction alone cannot reach material'):
        compile_cut_patch(before, {'plan_hash': digest(before.model_dump(mode='json')),
            'operation_id': 'cut', 'reversed': False}, {'operation_id': 'cut',
            'error_code': 'subtractive_feature_no_effect', 'details': {'cut_diagnostic_json': json.dumps({
                'schema_version': 'subtractive-evidence.v2', 'classification': reason})}})


@pytest.mark.parametrize('mutation', ['length', 'remove', 'target', 'direction'])
def test_no_effect_repair_preserves_plan_except_failed_direction(mutation):
    from app.freecad.constraint_repair import validate_subtractive_repair
    before = plan(); after = copy.deepcopy(before)
    if mutation == 'length': after['operations'][0]['args']['length_mm'] = 40
    if mutation == 'remove': after['operations'].pop(0)
    if mutation == 'target': after['operations'][0]['args']['profile'] = 'Other'
    if mutation == 'direction': after['operations'][0]['args']['reversed'] = False
    args = (FreeCADOperationPlan.model_validate(before), FreeCADOperationPlan.model_validate(after),
            {'error_code': 'subtractive_feature_no_effect', 'operation_id': 'cut'})
    if mutation == 'direction': validate_subtractive_repair(*args)
    else:
        with pytest.raises(ValueError): validate_subtractive_repair(*args)


def test_placement_repair_requires_independent_contract_and_keeps_dimensions():
    from app.freecad.constraint_repair import validate_subtractive_repair
    before=plan()
    before['operations'].insert(0,{'op_id':'profile','action':'sketch.create','args':{'name':'Profile'}})
    after=copy.deepcopy(before);after['operations'][0]['args']['offset_mm']=20
    failure={'error_code':'subtractive_feature_no_effect','operation_id':'cut'}
    with pytest.raises(ValueError,match='acceptance'):
        validate_subtractive_repair(FreeCADOperationPlan.model_validate(before),FreeCADOperationPlan.model_validate(after),failure)
    failure['engineering_acceptance']={'objective':'one solid','checks':[{
        'check_id':'connected','kind':'solid_count','nominal':1,'description':'connected','source_quote':'one solid'}]}
    validate_subtractive_repair(FreeCADOperationPlan.model_validate(before),FreeCADOperationPlan.model_validate(after),failure)
    after['operations'][1]['args']['length_mm']=20
    with pytest.raises(ValueError,match='dimensions'):
        validate_subtractive_repair(FreeCADOperationPlan.model_validate(before),FreeCADOperationPlan.model_validate(after),failure)


def test_cut_patch_is_idempotent_and_preserves_other_cut_and_export():
    from app.freecad.constraint_repair import compile_cut_patch
    from app.freecad.failure_snapshot import digest
    raw = plan()
    raw['operations'].insert(1, {'op_id': 'second-cut', 'action': 'feature.pocket',
                               'args': {'name': 'Other', 'profile': 'OtherProfile', 'length_mm': 12}})
    before = FreeCADOperationPlan.model_validate(raw)
    patch = {'plan_hash': digest(before.model_dump(mode='json')), 'operation_id': 'cut', 'reversed': False}
    failure = {'error_code': 'subtractive_feature_no_effect', 'operation_id': 'cut'}
    after = compile_cut_patch(before, patch, failure)
    assert after.operations[1:] == before.operations[1:]
    assert after.operations[0].args['length_mm'] == 38
    assert compile_cut_patch(before, copy.deepcopy(patch), failure) == after
    for bad in ({**patch, 'operation_id': 'second-cut'}, {**patch, 'plan_hash': '0'*64},
                {**patch, 'length_mm': 10}, {**patch, 'operations': []}):
        with pytest.raises(ValueError):
            compile_cut_patch(before, bad, failure)


@pytest.mark.parametrize('unsafe', ['shared-sweep-path', 'unverified-acceptance'])
def test_cut_placement_cannot_move_a_shared_path_or_use_incomplete_acceptance(unsafe):
    from app.freecad.constraint_repair import compile_cut_patch
    from app.freecad.failure_snapshot import digest
    raw = plan()
    raw['operations'].insert(0, {'op_id': 'create-profile', 'action': 'sketch.create', 'args': {'name': 'Profile'}})
    contract = {'objective': 'one solid', 'checks': [{'check_id': 'connected', 'kind': 'solid_count',
        'nominal': 1, 'description': 'connected', 'source_quote': 'one solid'}]}
    if unsafe == 'shared-sweep-path':
        raw['operations'].insert(-1, {'op_id': 'sweep', 'action': 'feature.sweep',
            'args': {'name': 'Sweep', 'profile': 'Section', 'path': 'Profile'}})
    else:
        contract['verification_limits'] = ['position has no measurable definition']
    before = FreeCADOperationPlan.model_validate(raw)
    with pytest.raises(ValueError, match='scoped acceptance'):
        compile_cut_patch(before, {'plan_hash': digest(before.model_dump(mode='json')), 'operation_id': 'cut',
            'placement': {'offset_mm': 1}}, {'operation_id': 'cut', 'error_code': 'subtractive_feature_no_effect',
            'engineering_acceptance': contract})


@pytest.mark.asyncio
async def test_cut_patch_rejection_is_saved_and_corrected_inside_provider_loop():
    from app.freecad.failure_snapshot import digest
    from app.freecad.operation_generator import FreeCADOperationGenerator
    from tests.test_freecad_agent_tools import ProviderTurns, call
    from tests.test_freecad_operation_generator import _provenance
    before = FreeCADOperationPlan.model_validate(plan())
    good = {'plan_hash': digest(before.model_dump(mode='json')), 'operation_id': 'cut', 'reversed': False}
    provider = ProviderTurns([call('freecad_patch_cut', {**good, 'operation_id': 'another'})],
                             [call('freecad_patch_cut', good)])
    rejected = []
    async def record(value):
        rejected.append(value)
    native = {'schema_version': 'subtractive-evidence.v2', 'coordinate_space': 'body',
        'classification': 'wrong_direction', 'intersection_volume_mm3': 0,
        'opposite_intersection_volume_mm3': 20}
    pending = {'schema_version': 'pending-profile-diagnostics.v1', 'valid_checkpoint': False, 'profiles': []}
    result = await FreeCADOperationGenerator(client=provider, provenance_reader=_provenance).repair(
        source_code=before.model_dump_json(), failure={'error_code': 'subtractive_feature_no_effect', 'operation_id': 'cut',
            'details': {'cut_diagnostic_json': json.dumps(native), 'pending_profiles_json': json.dumps(pending)}},
        base_state=None, output_formats=('fcstd', 'step'), rejection_sink=record)
    assert result.operation_plan.operations[0].args['reversed'] is False
    assert len(rejected) == 1 and rejected[0]['differences'][0]['field'] == 'operation_id'
    assert len(provider.requests) == 2
    payload = json.loads(provider.requests[0]['messages'][1]['content'])
    assert payload['measured_cut'] == native and payload['pending_profile_diagnostics'] == pending
    assert {t['function']['name'] for t in provider.requests[0]['tools']} == {'freecad_patch_cut', 'freecad_describe_operation'}


@pytest.mark.asyncio
async def test_cut_schema_lookups_consume_explicit_tool_budget(monkeypatch):
    from app.config import settings
    from app.freecad.operation_generator import FreeCADOperationGenerator
    from tests.test_freecad_agent_tools import ProviderTurns, call
    from tests.test_freecad_operation_generator import _provenance
    monkeypatch.setattr(settings, 'cut_repair_max_tool_calls', 1)
    provider = ProviderTurns([call('freecad_describe_operation', {'action': 'sketch.create'})])
    with pytest.raises(ValueError, match='tool-call budget exhausted') as raised:
        await FreeCADOperationGenerator(client=provider, provenance_reader=_provenance).repair(
            source_code=FreeCADOperationPlan.model_validate(plan()).model_dump_json(),
            failure={'error_code': 'subtractive_feature_no_effect', 'operation_id': 'cut'},
            base_state=None, output_formats=('fcstd', 'step'))
    assert raised.value.code == 'cut_repair_budget_exhausted'
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_cut_repair_against_checkpoint_preserves_all_persisted_operation_ids():
    from app.freecad.failure_snapshot import digest
    from app.freecad.operation_generator import FreeCADOperationGenerator
    from tests.test_freecad_agent_tools import ProviderTurns, call
    from tests.test_freecad_operation_generator import _provenance
    before = FreeCADOperationPlan.model_validate(plan())
    provider = ProviderTurns([call('freecad_patch_cut', {'plan_hash': digest(before.model_dump(mode='json')),
        'operation_id': 'cut', 'reversed': False})])
    result = await FreeCADOperationGenerator(client=provider, provenance_reader=_provenance).repair(
        source_code=before.model_dump_json(), failure={'error_code': 'subtractive_feature_no_effect', 'operation_id': 'cut'},
        base_state={'objects': []}, output_formats=('fcstd', 'step'))
    assert [op.op_id for op in result.operation_plan.operations] == [op.op_id for op in before.operations]
    assert result.operation_plan.operations[-1] == before.operations[-1]
