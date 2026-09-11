import json

import pytest
from pydantic import ValidationError

from app.freecad.inspection import InspectionRequest, inspect_state
from app.freecad.operation_generator import FreeCADOperationGenerator
from tests.test_freecad_operation_generator import _client, _provenance, _valid_plan


def test_inspection_is_bounded_and_reports_unknown_and_unrecorded_data():
    state={'objects':[{'name':'Hole','properties':{f'p{i}':'x'*4000 for i in range(100)}}]}
    query=InspectionRequest(objects=['Hole','missing'],fields=['properties','constraints'],limit=32)
    result=inspect_state(state,query)
    assert result['objects'][0]['properties']['omitted']>0
    assert len(json.dumps(result))<19000
    assert result['objects'][0]['constraints']['status']=='unavailable'
    assert result['objects'][1]['error']=='object_not_found'
    assert len(state['objects'][0]['properties']['p0'])==4000
    with pytest.raises(ValidationError):
        InspectionRequest(objects=['Hole'],fields=['execute_python'])


@pytest.mark.asyncio
async def test_agent_executes_inspection_before_returning_existing_model_plan():
    query={'inspect':{'objects':['Pad'],'fields':['properties']}}
    client=_client([query,_valid_plan()])
    state={'objects':[{'name':'Pad','properties':{'Length':'10.00 mm'}}]}
    result=await FreeCADOperationGenerator(client=client,provenance_reader=_provenance)._complete(
        user_payload={'task':'modify'},generator_kind='freecad_operations',
        output_formats=('step','stl'),base_state=state)
    evidence=result.provenance['inspection_calls'][0]['result']
    assert evidence['objects'][0]['properties']['items']==[{'name':'Length','value':'10.00 mm'}]
    assert '10.00 mm' in client.completions.kwargs[1]['messages'][-1]['content']
    assert client.completions.calls==2


@pytest.mark.asyncio
async def test_agent_cannot_silently_skip_required_inspection():
    client=_client([_valid_plan(),_valid_plan()])
    with pytest.raises(ValueError,match='did not return a valid plan'):
        await FreeCADOperationGenerator(client=client,provenance_reader=_provenance)._complete(
            user_payload={'task':'modify'},generator_kind='freecad_operations',
            output_formats=('step','stl'),base_state={'objects':[]})


@pytest.mark.asyncio
async def test_rejected_early_plan_can_recover_through_checkpoint_inspection():
    early_plan=_valid_plan()
    query={'inspect':{'objects':['Pad'],'fields':['properties']}}
    client=_client([early_plan,query,_valid_plan()])
    state={'objects':[{'name':'Pad','properties':{'Length':'10.00 mm'}}]}
    result=await FreeCADOperationGenerator(client=client,provenance_reader=_provenance)._complete(
        user_payload={'task':'modify'},generator_kind='freecad_operations',
        output_formats=('step','stl'),base_state=state)
    assert client.completions.calls==3
    messages=client.completions.kwargs[-1]['messages']
    assert '"inspect"' in json.loads(messages[1]['content'])['next_response']
    assert json.loads(messages[2]['content'])==early_plan
    assert messages[2]['role']=='assistant'
    assert 'Do not return an operation plan' in messages[3]['content']
    measurement=json.loads(messages[-1]['content'])
    assert measurement['inspection_result']['objects'][0]['properties']['items']==[
        {'name':'Length','value':'10.00 mm'}]
    assert measurement['remaining_inspection_queries']==2
    assert result.provenance['inspection_calls'][0]['request']['objects']==['Pad']
    assert result.provenance['inspection_calls'][0]['result']==measurement['inspection_result']
