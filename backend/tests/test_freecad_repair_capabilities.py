import pytest
from app.freecad.repair_capabilities import resolve_repair_capability


@pytest.mark.parametrize('action,repairable', [('feature.pocket', True), ('feature.hole', True),
    ('feature.loft', False), ('feature.sweep', False), ('feature.revolve', False)])
def test_executable_subtractive_operations_have_explicit_repair_boundaries(action, repairable):
    capability = resolve_repair_capability(action, {'error_code': 'subtractive_feature_no_effect',
        'operation_id': 'cut'}, {'failed_operation': {'op_id': 'cut', 'action': action}})
    assert capability.executable and capability.diagnosable
    assert capability.repairable == repairable
    assert capability.block_reason == (None if repairable else 'unsupported_repair')
    assert capability.allowed_fields == (('reversed',) if repairable else ())


def test_missing_evidence_and_protected_scope_are_not_user_missing_parameters():
    missing = resolve_repair_capability('feature.pocket', {'operation_id': 'cut'}, {})
    assert not missing.repairable and missing.block_reason == 'missing_diagnostic'
    locked = resolve_repair_capability('feature.pocket', {'operation_id': 'cut', 'details': {'direction_locked': True}},
        {'failed_operation': {'op_id': 'cut', 'action': 'feature.pocket'}})
    assert not locked.repairable and locked.block_reason == 'scope_protected'


@pytest.mark.asyncio
@pytest.mark.parametrize('action,args', [
    ('feature.loft',{'profiles':['SectionA','SectionB']}),
    ('feature.sweep',{'profile':'SectionA','path':'Path'}),
    ('feature.revolve',{'profile':'SectionA','axis':'z','angle_deg':360}),
])
async def test_unsupported_cut_patch_stops_before_provider_request(action,args):
    from app.freecad.contracts import FreeCADOperationPlan, FreeCADPlanningError
    from app.freecad.operation_generator import FreeCADOperationGenerator
    from tests.test_freecad_agent_tools import ProviderTurns
    provider=ProviderTurns()
    plan=FreeCADOperationPlan.model_validate({'operations':[
        {'op_id':'cut','action':action,'args':{'name':'Cut','subtractive':True,**args}},
        {'op_id':'export','action':'document.export','args':{'formats':['fcstd','step']}}]})
    with pytest.raises(FreeCADPlanningError) as stopped:
        await FreeCADOperationGenerator(client=provider).repair(source_code=plan.model_dump_json(),
            failure={'operation_id':'cut','error_code':'subtractive_feature_no_effect','error_message':'No material removed'},
            base_state=None,output_formats=('step',))
    assert stopped.value.code=='repair_capability_unsupported'
    assert 'No material removed' in str(stopped.value) and provider.requests==[]
