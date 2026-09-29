"""Recovery must retain every feature and dimension, not just get an exit code."""
import copy
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
