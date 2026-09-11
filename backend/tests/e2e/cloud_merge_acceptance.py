"""Two real branches, independent edits, three-way merge and conflicting writes."""
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
from cloud_document_acceptance import call, wait_task, commit


def values(document):
    return {p['id']:p['value'] for f in document['features'] for p in f['parameters']}


def modify(client, document_id, parameter, value):
    path='/api/documents/'+document_id
    doc=call(client,'GET',path)
    result=call(client,'POST',path+'/operations',expected=202,json={
        'action':'parameters.update','expected_base_revision_id':doc['head_revision_id'],
        'expected_state_version':doc['state_version'],'idempotency_key':str(uuid4()),
        'modification':{'schema_version':'freecad-structured-modification.v1',
            'expected_state_sha256':doc['parameter_state_sha256'],
            'parameter_updates':[{'parameter_id':parameter,'value':value}]}})
    commit(client,wait_task(client,result['workflow_run_id']))
    updated=call(client,'GET',path)
    assert values(updated)[parameter]==value
    return updated


def merge_body(comparison, mode='parameters'):
    return {**{key:comparison[key] for key in ('source_document_id','source_revision_id','source_state_version',
        'target_revision_id','target_state_version','comparison_hash')},'mode':mode,'idempotency_key':str(uuid4())}


def main():
    private=json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    fixture=json.loads(Path('/tmp/cad-expansion-branch-document.json').read_text())
    client=httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']})
    target,source=fixture['source_document_id'],fixture['document_id']
    target_path='/api/documents/'+target
    base=call(client,'GET',target_path)
    a,b=values(base)['PadA.Length']+1,values(base)['PadB.Length']+2
    target_doc=modify(client,target,'PadA.Length',a)
    source_doc=modify(client,source,'PadB.Length',b)
    comparison=call(client,'GET',target_path+'/compare/'+source)
    assert comparison['common_revision_id']==fixture['source_revision_id'],comparison
    assert comparison['can_merge_parameters'],comparison['conflicts']
    assert comparison['parameters']['updates']==[{'parameter_id':'PadB.Length','value':b}]
    body=merge_body(comparison)
    submitted=call(client,'POST',target_path+'/merges',expected=202,json=body)
    assert call(client,'POST',target_path+'/merges',expected=202,json=body)['workflow_run_id']==submitted['workflow_run_id']
    call(client,'POST',target_path+'/merges',expected=409,json={**body,'mode':'source_geometry'})
    # The authoritative target stays unchanged until reviewed commit.
    assert call(client,'GET',target_path)['head_revision_id']==target_doc['head_revision_id']
    commit(client,wait_task(client,submitted['workflow_run_id']))
    merged=call(client,'GET',target_path)
    assert values(merged)['PadA.Length']==a and values(merged)['PadB.Length']==b
    assert call(client,'GET','/api/documents/'+source)['head_revision_id']==source_doc['head_revision_id']
    replay=call(client,'POST',target_path+'/merges',expected=202,json=body)
    assert replay['replayed'] and replay['workflow_run_id']==submitted['workflow_run_id']
    after=call(client,'GET',target_path+'/compare/'+source)
    assert after['common_revision_id']==source_doc['head_revision_id'] and not after['can_merge_parameters']
    print('independent parameter merge and durable replay passed',{'workflow_id':submitted['workflow_run_id'],'values':values(merged)},flush=True)
    # Source changes the parameter already edited independently on target.
    source_doc=modify(client,source,'PadA.Length',a+4)
    conflict=call(client,'GET',target_path+'/compare/'+source)
    assert not conflict['can_merge_parameters'] and conflict['conflicts'],conflict
    call(client,'POST',target_path+'/merges',expected=409,json=merge_body(conflict))
    assert call(client,'GET',target_path)['head_revision_id']==merged['head_revision_id']
    Path('/tmp/cad-expansion-merge-evidence.json').write_text(json.dumps({'target_document_id':target,
        'source_document_id':source,'tenant_id':fixture['tenant_id'],'merged_workflow_id':submitted['workflow_run_id'],
        'merged_revision_id':merged['head_revision_id'],'conflict_comparison':conflict},ensure_ascii=False,indent=2))
    print('same-parameter conflict rejected; target checkpoint preserved',flush=True)
    client.close()


if __name__=='__main__':
    main()
