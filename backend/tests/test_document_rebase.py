from copy import deepcopy

import pytest

from app.services.document_rebase import prove_independent_parameter_edit,ParameterRebaseConflict
from app.services.feature_leases import related


def checkpoint():
    return {'parameter_state_sha256':'a'*64,'features':[
        {'id':'origin','kernel_name':'Origin','label':'Origin','type':'App::Origin','dependencies':[],'parameters':[]},
        *[{'id':name,'kernel_name':name,'label':name,'type':'PartDesign::Pad','dependencies':['origin'],
           'parameters':[{'id':name+'.Length','value':10,'editable':True}],
           'geometry_sha256':'b'*64,'geometry_fingerprint_kind':'fcstd-brep.v1',
           'shape':{'volume':100},'is_valid':True} for name in ('PadA','PadB')]]}


def test_independent_parameter_edits_can_replay_despite_shared_origin():
    before=checkpoint();after=deepcopy(before)
    after['features'][2]['parameters'][0]['value']=12
    after['features'][2]['geometry_sha256']='c'*64
    proof=prove_independent_parameter_edit(before,after,[{'parameter_id':'PadA.Length','value':15}])
    assert proof['checked_feature_ids']==['PadA','origin']
    assert not related(before['features'],'PadA','PadB')
    assert related(before['features'],'PadA','origin')


def test_dependency_geometry_change_blocks_replay_even_with_same_volume():
    before=checkpoint();before['features'][1]['dependencies']=['PadB']
    after=deepcopy(before);after['features'][2]['geometry_sha256']='c'*64
    with pytest.raises(ParameterRebaseConflict,match='依赖已改变'):
        prove_independent_parameter_edit(before,after,[{'parameter_id':'PadA.Length','value':15}])


def test_missing_geometry_fingerprints_and_parameter_changes_cannot_claim_safe_replay():
    before=checkpoint();after=deepcopy(before)
    after['features'][1]['parameters'][0]['value']=11
    with pytest.raises(ParameterRebaseConflict):
        prove_independent_parameter_edit(before,after,[{'parameter_id':'PadA.Length','value':15}])
    before['features'][1].pop('geometry_sha256');after=deepcopy(before)
    with pytest.raises(ParameterRebaseConflict,match='缺少完整几何指纹'):
        prove_independent_parameter_edit(before,after,[{'parameter_id':'PadA.Length','value':15}])


@pytest.mark.parametrize('field', ['global_placement', 'sketch_constraints_sha256', 'geometry_fingerprint_kind'])
def test_placement_constraint_and_legacy_fingerprint_changes_cannot_rebase(field):
    before=checkpoint();after=deepcopy(before)
    after['features'][1][field]='changed'
    with pytest.raises(ParameterRebaseConflict):
        prove_independent_parameter_edit(before,after,[{'parameter_id':'PadA.Length','value':15}])
