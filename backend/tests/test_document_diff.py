"""Semantic merge invariants; runtime integration is in cloud_merge_acceptance."""
from copy import deepcopy

import pytest

from app.services.document_diff import common_ancestor, semantic_changes, parameter_merge_proposal
from app.services.document_rebase import ParameterRebaseConflict


def state(a=10, b=10):
    return {'parameter_state_sha256':f'{a}-{b}', 'features':[
        {'id':name,'kernel_name':name,'label':name,'type':'PartDesign::Pad','dependencies':[],
         'parameters':[{'id':name+'.Length','value':value,'editable':True}],
         'geometry_sha256':str(value),'geometry_fingerprint_kind':'fcstd-brep.v1','shape':{'volume':value}}
        for name,value in [('A',a),('B',b)]]}


def node(parent=None, **extra):
    return {'parent_revision_id':parent, **extra}


def typed(parent, parameter, value):
    return node(parent,action='parameters.update',arguments={'structured_modification':{
        'parameter_updates':[{'parameter_id':parameter,'value':value}]}})


def test_common_ancestor_follows_fork_and_second_merge_parent():
    graph={'a':node(),'b':node('a'),'seed':node(fork_source='a'),'fork':node('seed'),
           'edit':node('fork'),'merge':node('b',merge_source='edit')}
    assert common_ancestor(graph,'b','edit')=='a'
    assert common_ancestor(graph,'merge','edit')=='edit'


def test_incomplete_and_ambiguous_history_cannot_choose_a_merge_base():
    with pytest.raises(ValueError,match='不完整'):
        common_ancestor({'a':node('missing')},'a','a')
    graph={'a':node(),'b':node(),'c':node('a',merge_source='b'),'d':node('b',merge_source='a')}
    with pytest.raises(ValueError,match='多个'):
        common_ancestor(graph,'c','d')


def test_independent_parameter_replay_preserves_target_edits_and_drops_already_applied_values():
    graph={'base':node(),'source':typed('base','B.Length',12)}
    proposal=parameter_merge_proposal(graph,'base','source',state(),state(b=12),state(a=14))
    assert proposal['updates']==[{'parameter_id':'B.Length','value':12}]
    assert proposal['proof']['checked_feature_ids']==['B']
    assert parameter_merge_proposal(graph,'base','source',state(),state(b=12),state(a=14,b=12))['updates']==[]


def test_same_parameter_and_changed_dependency_are_conflicts():
    graph={'base':node(),'source':typed('base','B.Length',12)}
    with pytest.raises(ParameterRebaseConflict):
        parameter_merge_proposal(graph,'base','source',state(),state(b=12),state(b=14))
    base,source,target=state(),state(b=12),state(a=14)
    for item in (base,source,target):
        item['features'][1]['dependencies']=['A']
    with pytest.raises(ParameterRebaseConflict):
        parameter_merge_proposal(graph,'base','source',base,source,target)


def test_generic_operations_and_unexplained_parameter_changes_are_never_assumed_safe():
    graph={'base':node(),'source':node('base',action='modify',arguments={})}
    with pytest.raises(ParameterRebaseConflict,match='非参数'):
        parameter_merge_proposal(graph,'base','source',state(),state(b=12),state(a=14))
    graph['source']=typed('base','A.Length',12)
    with pytest.raises(ParameterRebaseConflict,match='操作证据'):
        parameter_merge_proposal(graph,'base','source',state(),state(b=12),state(a=14))


def test_fork_restore_is_only_transparent_when_immutable_source_matches():
    graph={'base':node(),'seed':node(fork_source='base'),'fork':node('seed',fork_workflow='wf',
        source_workflow_run_id='wf',branch_source_revision='base',arguments={
            'operation_context':{'rule':'explicit_branch_fork'},'revision_restore':{'source_revision_id':'base'}}),
        'source':typed('fork','B.Length',12)}
    assert parameter_merge_proposal(graph,'base','source',state(),state(b=12),state(a=14))['updates']
    graph['fork']['arguments']['revision_restore']['source_revision_id']='wrong'
    with pytest.raises(ParameterRebaseConflict):
        parameter_merge_proposal(graph,'base','source',state(),state(b=12),state(a=14))


def test_semantic_diff_reports_exact_parameter_and_geometry_changes_without_ledger_noise():
    before,after=state(),state(b=12)
    after['features'].append({'id':'ledger','kernel_name':'CADAgentLedger'})
    changes=semantic_changes(before,after)
    assert len(changes)==1 and changes[0]['kernel_name']=='B'
    assert {f['name'] for f in changes[0]['fields']}=={'parameters','geometry_sha256'}
    assert changes[0]['fields'][0]['after'][0]['value']==12
    after=deepcopy(before);after['features'].pop()
    assert semantic_changes(before,after)[0]['kind']=='removed'
