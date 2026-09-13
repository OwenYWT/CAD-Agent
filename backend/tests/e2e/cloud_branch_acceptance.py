"""Actual branch fork, native checkpoint, review/commit and lineage acceptance."""
import json
import os
from pathlib import Path
from acceptance_paths import evidence_path
from uuid import uuid4

import httpx
from cloud_document_acceptance import call, wait_task, commit


def main():
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    fixture = json.loads(Path(str(evidence_path('cad-expansion-collaboration-document.json'))).read_text())
    client = httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']})
    path = '/api/documents/'+fixture['document_id']
    source = call(client,'GET',path)
    feature = next(f for f in source['features'] if f['kernel_name']=='PadA')
    call(client,'PUT',path+'/features/'+feature['id']+'/annotation',json={
        'annotation_id':str(uuid4()),'revision_id':source['head_revision_id'],
        'expected_version':feature.get('annotation_version',0),'role':'分支验收基准', 'intent':'跨分支保留部件用途'})
    body = {'name':'branch-acceptance-'+uuid4().hex[:8],'expected_revision_id':source['head_revision_id'],
        'expected_state_version':source['state_version'],'idempotency_key':str(uuid4())}
    created = call(client,'POST',path+'/branches',expected=202,json=body)
    Path(str(evidence_path('cad-expansion-branch-document.json'))).write_text(json.dumps({**created,'source_document_id':fixture['document_id'],
        'source_revision_id':source['head_revision_id'],'source_state_version':source['state_version']}))
    print('fork accepted',created,flush=True)
    replay = call(client,'POST',path+'/branches',expected=202,json=body)
    assert replay['replayed'] and replay['workflow_run_id']==created['workflow_run_id']
    call(client,'POST',path+'/branches',expected=409,json={**body,'name':body['name']+'-conflict'})
    task = wait_task(client,created['workflow_run_id'])
    commit(client,task)
    result = call(client,'GET','/api/documents/'+created['document_id'])
    assert result['state_version']==1 and result['fcstd'] and result['parameter_state_sha256']
    assert {f['id'] for f in source['features']}=={f['id'] for f in result['features']}
    inherited = next(f for f in result['features'] if f['id']==feature['id'])
    assert inherited['role']=='分支验收基准' and inherited['annotation_version']==feature.get('annotation_version',0)+1
    values = lambda d: {p['id']:p['value'] for f in d['features'] for p in f['parameters']}
    assert values(source)==values(result)
    assert call(client,'GET',path)['head_revision_id']==source['head_revision_id']
    listing = call(client,'GET',path+'/branches')
    assert {source['document_id'],result['document_id']} <= {b['document_id'] for b in listing['branches']}
    print('branch native validation, commit, idempotency, stable feature IDs and annotation inheritance passed',flush=True)
    client.close()


if __name__=='__main__':
    main()
