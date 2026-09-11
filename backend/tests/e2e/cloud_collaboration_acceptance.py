"""Real HTTP multi-user leases, parameter rebase, Agent and revision acceptance."""
import json
import os
from pathlib import Path
import secrets
import threading
import time
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from cloud_document_acceptance import call,wait_task,commit

PRIVATE=Path(os.environ['CAD_NATIVE_E2E_PRIVATE'])
MODEL=Path('/tmp/cad-expansion-collaboration-document.json')
REPORT=Path('/tmp/cad-expansion-collaboration-http.json')


def main():
    private=json.loads(PRIVATE.read_text());model=json.loads(MODEL.read_text())
    owner=httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']})
    if 'editor' not in private:
        config=dotenv_values(os.environ['CAD_NATIVE_E2E_ENV']);admin=httpx.Client()
        auth=call(admin,'POST','/api/auth/login/password',json={'phone':'admin','password':config['ADMIN_PASSWORD']})
        admin.headers['Authorization']='Bearer '+auth['token']
        invitation=call(admin,'POST','/api/auth/invites',json={'max_uses':1})
        phone='189'+str(time.time_ns()%100000000).zfill(8);password=secrets.token_urlsafe(24)
        person=call(admin,'POST','/api/auth/register/invite',json={'phone':phone,'password':password,'invite_code':invitation['code']})
        private['editor']={**person,'phone':phone,'password':password};PRIVATE.write_text(json.dumps(private))
    editor=httpx.Client(headers={'Authorization':'Bearer '+private['editor']['token']})
    doc='/api/documents/'+model['document_id']
    invite=call(owner,'POST',doc+'/invitations?role=editor',expected=201)
    accepted=call(editor,'POST',doc+'/invitations/accept',json={'tenant_id':model['tenant_id'],'token':invite['token']})
    assert accepted['role']=='editor'
    editor.headers['X-Workspace-Tenant']=model['tenant_id']
    base=call(owner,'GET',doc);editor_view=call(editor,'GET',doc)
    assert editor_view['can_edit'] and not editor_view['can_share'] and not editor_view['can_commit']
    features={f['kernel_name']:f for f in base['features']}
    assert features['PadA']['parameters'][0]['value']==10
    assert features['PadB']['parameters'][0]['value']==10
    assert features['PadA']['geometry_sha256'] and features['PadB']['geometry_sha256']
    def lease(client,name,client_id=None):
        return call(client,'POST',doc+'/leases',json={'feature_id':features[name]['id'],'revision_id':base['head_revision_id'],'client_id':client_id or str(uuid4())})
    client_a=str(uuid4())
    lease_a=lease(owner,'PadA',client_a);lease_b=lease(editor,'PadB')
    for name in ('PadA','SketchA'):
        call(editor,'POST',doc+'/leases',expected=409,json={'feature_id':features[name]['id'],
            'revision_id':base['head_revision_id'],'client_id':str(uuid4())})
    call(owner,'DELETE',doc+'/leases/'+lease_b['token'],expected=403)
    assert len(call(owner,'GET',doc+'/collaboration')['leases'])==2
    report={'independent_leases':True,'dependency_lease_conflict':True,'foreign_release_denied':True,'editor_has_no_commit_or_share':True}
    REPORT.write_text(json.dumps(report,indent=2));print('Independent leases and dependency conflicts passed',flush=True)
    def edit(name,value,token):
        return {'action':'parameters.update','expected_base_revision_id':base['head_revision_id'],
            'expected_state_version':base['state_version'],'allow_rebase':True,'lease_token':token,'idempotency_key':str(uuid4()),
            'modification':{'expected_state_sha256':base['parameter_state_sha256'],
                'parameter_updates':[{'parameter_id':name+'.Length','value':value}]}}
    # B commits first while A's original draft and feature lease are still held.
    # The draft is actively held while B runs a real provider/kernel workflow;
    # renew through the same public API the browser uses, without altering TTL.
    stopped=threading.Event();renewal_errors=[]
    def renew_a():
        while not stopped.wait(20):
            try:
                call(owner,'POST',doc+'/leases',json={'feature_id':features['PadA']['id'],
                    'revision_id':base['head_revision_id'],'client_id':client_a,'lease_token':lease_a['token']})
            except Exception as error:
                renewal_errors.append(error);return
    renewing=threading.Thread(target=renew_a,daemon=True);renewing.start()
    try:
        request_b=edit('PadB',13,lease_b['token'])
        submitted_b=call(editor,'POST',doc+'/operations',expected=202,json=request_b)
        task_b=wait_task(editor,submitted_b['workflow_run_id']);assert task_b['status']=='succeeded',{k:task_b.get(k) for k in ('error_code','error_message','error')}
        change_b=task_b['change_set']['id']
        call(editor,'POST',f'/api/change-sets/{change_b}/accept',json={'note':'Independent native body B checked'})
        call(editor,'POST',f'/api/change-sets/{change_b}/commit',expected=403)
        call(owner,'POST',f'/api/change-sets/{change_b}/commit')
    finally:
        stopped.set();renewing.join(timeout=65)
    assert not renewing.is_alive() and not renewal_errors,renewal_errors
    middle=call(owner,'GET',doc)
    assert middle['state_version']==base['state_version']+1
    # A explicitly permits rebase; the server proves all its read dependencies
    # unchanged, executes on B's new FCStd and revalidates before normal review.
    request_a=edit('PadA',12,lease_a['token'])
    submitted_a=call(owner,'POST',doc+'/operations',expected=202,json=request_a)
    assert submitted_a['rebased_from_revision_id']==base['head_revision_id']
    task_a=wait_task(owner,submitted_a['workflow_run_id']);commit(owner,task_a)
    final=call(owner,'GET',doc)
    parameters={p['id']:p['value'] for f in final['features'] for p in f['parameters']}
    assert parameters['PadA.Length']==12 and parameters['PadB.Length']==13
    assert final['state_version']==base['state_version']+2
    replay=call(owner,'POST',doc+'/operations',expected=202,json=request_a)
    assert replay['workflow_run_id']==submitted_a['workflow_run_id'] and replay['replayed']
    conflicting=edit('PadB',14,None)
    call(owner,'POST',doc+'/operations',expected=409,json=conflicting)
    operations=call(owner,'GET',doc+'/collaboration')['operations']
    assert len(operations)==3,operations
    report.update(rebase_reexecuted_and_validated=True,rebase_idempotent_after_commit=True,
        same_parameter_conflict_rejected=True,final_parameters=parameters,state_version=final['state_version'],
        document_id=model['document_id'],workflow_ids=[submitted_b['workflow_run_id'],submitted_a['workflow_run_id']])
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False),flush=True)


main()
