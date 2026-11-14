from copy import deepcopy
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.contracts.native_references import NativeReferenceError, digest, reference_id, selectors, validate_catalog, verify_transition
from app.freecad.inspection import InspectionRequest, inspect_state
from app.freecad.contracts import FreeCADOperationPlan
from app.topology.contracts import FREECAD_SELECTOR, FreeCADTopologySelector


def catalog_and_plan():
    plan={'operations':[{'op_id':'pad','action':'feature.pad','args':{'name':'Pad','profile':'Profile','length_mm':12}}]}
    origin={'plan_sha256':digest(plan),'op_id':'pad'}
    incarnation=digest([origin,'Pad'])
    ref={'reference_id':reference_id(incarnation,'edge','#native-edge'), 'mapped_name':'#native-edge',
         'subelement_kind':'edge','geometry_type':'Part::GeomCircle','center_mm':[10,15,12],'radius_mm':3}
    catalog={'schema_version':'native-topology.v1','fcstd_sha256':'f'*64,'uses':[], 'objects':{
        'Pad':{'origin':origin,'incarnation':incarnation,'native_id':7,'type_id':'PartDesign::Pad',
               'map_version':'15.70200.5','references':[ref],'coverage':{'total':1,'recorded':1,'unavailable':0}}}}
    return catalog,plan


def test_native_selector_carries_only_published_identity_and_v1_is_unchanged():
    catalog,_=catalog_and_plan()
    payload=selectors(catalog,'Pad',UUID(int=1))[0]
    parsed=FREECAD_SELECTOR.validate_python(payload)
    assert parsed.model_dump(mode='json') == payload
    for key in ('mapped_name','subelement_name','confidence','allow_ambiguous'):
        with pytest.raises(ValidationError):FREECAD_SELECTOR.validate_python({**payload,key:'injected'})
    v1=FreeCADTopologySelector(revision_id=UUID(int=1),object_name='Pad',subelement_kind='face',geometry='planar',axis='z',extreme='max')
    assert FREECAD_SELECTOR.validate_python(v1.model_dump(mode='json')).model_dump_json() == v1.model_dump_json()


def test_reference_catalog_and_creation_transition_are_bound_to_source():
    catalog,plan=catalog_and_plan()
    verify_transition(catalog,plan)
    with pytest.raises(NativeReferenceError,match='another FCStd'):validate_catalog(catalog,'e'*64)
    changed=deepcopy(catalog);changed['objects']['Pad']['references'][0]['mapped_name']='#other'
    with pytest.raises(NativeReferenceError):validate_catalog(changed)
    changed=deepcopy(catalog)
    owner=changed['objects']['Pad'];owner['origin']['op_id']='forged'
    owner['incarnation']=digest([owner['origin'],'Pad'])
    ref=owner['references'][0];ref['reference_id']=reference_id(owner['incarnation'],'edge',ref['mapped_name'])
    validate_catalog(changed)
    with pytest.raises(NativeReferenceError,match='executed typed plan'):verify_transition(changed,plan)
    with pytest.raises(NativeReferenceError,match='Native programs'):verify_transition(catalog,{'operations':[{'action':'api.execute'}]})
    missing=deepcopy(catalog);missing['objects']={}
    with pytest.raises(NativeReferenceError,match='creation inventory'):verify_transition(missing,plan)
    malformed=deepcopy(catalog);malformed['objects']['Pad']['references'][0]['center_mm'][0]=True
    with pytest.raises(NativeReferenceError,match='invalid measurement'):validate_catalog(malformed)


def test_native_inspection_publishes_opaque_selectors_from_verified_state_only():
    catalog,_=catalog_and_plan()
    state={'revision_id':str(UUID(int=1)),'objects':[{'name':'Pad','type_id':'PartDesign::Pad'}], 'native_topology':catalog}
    from app.freecad.semantic_state import project_semantic_state
    historical=project_semantic_state(UUID(int=2),{k:v for k,v in state.items() if k!='native_topology'},revision_id=UUID(int=1))
    extended=project_semantic_state(UUID(int=2),state,revision_id=UUID(int=1))
    assert historical['features'][0]['content_sha256']==extended['features'][0]['content_sha256']
    response=inspect_state(state,InspectionRequest(objects=['Pad'],fields=['references']))
    row=response['objects'][0]['references']
    assert row['status']=='measured' and row['total']==1
    assert row['items'][0]['selector'] == selectors(catalog,'Pad',UUID(int=1))[0]
    assert 'mapped_name' not in row['items'][0]
    del state['revision_id']
    from app.execution.canonical import canonical_sha256
    before=deepcopy(state)
    bound=inspect_state(state,InspectionRequest(objects=['Pad'],fields=['references']),revision_id=UUID(int=1))
    assert bound['objects'][0]['references']['items']==row['items']
    assert bound['state_hash']==canonical_sha256(before) and state==before
    state['revision_id']=str(UUID(int=1))
    catalog['objects']['Pad']['coverage']={'total':3,'recorded':1,'unavailable':2}
    page=inspect_state(state,InspectionRequest(objects=['Pad'],fields=['references']))['objects'][0]['references']
    assert page['total']==3 and page['recorded']==1 and page['omitted']==2
    catalog['objects']={}
    assert inspect_state(state,InspectionRequest(objects=['Pad'],fields=['references']))['objects'][0]['references']['status']=='unavailable'
    del state['native_topology']
    assert inspect_state(state,InspectionRequest(objects=['Pad'],fields=['references']))['objects'][0]['references']['status']=='unavailable'


def test_native_selector_is_accepted_in_actual_tool_plan():
    catalog,_=catalog_and_plan()
    selector=selectors(catalog,'Pad',UUID(int=1))[0]
    plan=FreeCADOperationPlan.model_validate({'operations':[
        {'op_id':'rim','action':'feature.chamfer','args':{'name':'Chamfer','target':'Pad','size_mm':.5,'use_all_edges':False,'selector':selector}},
        {'op_id':'export','action':'document.export','args':{'formats':['fcstd']}}]})
    assert plan.operations[0].typed_args().selector.reference_id == selector['reference_id']


def test_native_selection_uses_complete_verified_catalog_not_display_summary():
    from app.domain.selection import SelectionContextV1
    from app.freecad.selection import SelectionError, freeze_selection
    catalog,_=catalog_and_plan()
    selector=selectors(catalog,'Pad',UUID(int=1))[0]
    selection=SelectionContextV1(revision_id=UUID(int=1),state_version=1,
        feature_ids=[UUID(int=2)],topology_selector=selector)
    projection={'revision_id':str(UUID(int=1)),'fcstd':{'sha256':catalog['fcstd_sha256']},'features':[
        {'id':str(UUID(int=2)),'kernel_name':'Pad','label':'Pad','type':'PartDesign::Pad',
         'topology_bindings':[],'native_topology_bindings':[]}]}
    args={'revision_id':UUID(int=1),'state_version':1,'objective':'Chamfer the selected edge'}
    frozen=freeze_selection(projection,selection,**args,native_catalog=catalog)
    assert frozen['topology_selector']==selector
    with pytest.raises(SelectionError):freeze_selection(projection,selection,**args)
    wrong_file=deepcopy(catalog);wrong_file['fcstd_sha256']='e'*64
    with pytest.raises(NativeReferenceError):freeze_selection(projection,selection,**args,native_catalog=wrong_file)
    forged={**selector,'reference_id':'0'*64}
    projection['features'][0]['native_topology_bindings']=[forged]
    with pytest.raises(SelectionError):
        freeze_selection(projection,selection.model_copy(update={'topology_selector':FREECAD_SELECTOR.validate_python(forged)}),
                         **args,native_catalog=catalog)
