"""Actual worker outage/recovery, cancellation and shared-document authorization."""
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
from cloud_document_acceptance import BASE, PRIVATE, call, wait_task

PODMAN='/opt/homebrew/bin/podman'
WORKER=os.getenv('CAD_NATIVE_E2E_WORKER','cad-native-expansion-worker')


def main():
    private=json.loads(PRIVATE.read_text())
    owner=httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']})
    doc_id=private['document_id'];path='/api/documents/'+doc_id;before=call(owner,'GET',path)
    evidence=json.loads(Path('/tmp/cad-expansion-engineering-http.json').read_text())
    result=call(owner,'GET',path+'/engineering/'+evidence['workflow_run_id'])
    def payload():
        return {'expected_revision_id':before['head_revision_id'],'expected_state_version':before['state_version'],
            'idempotency_key':str(uuid4()),'task':{'kind':'linear_static','component_name':result['report']['component_name'],
                'material':{'name':'Controls acceptance material','young_modulus_mpa':210000,'poisson_ratio':0.3},
                'mesh_size_mm':4,'fixed_face':{'axis':'z','side':'min'},'loaded_face':{'axis':'z','side':'max'},'force_n':[0,0,500]}}
    viewer=httpx.Client(headers={'Authorization':'Bearer '+private['guest']['token']})
    call(viewer,'GET',path+'/engineering/'+evidence['workflow_run_id'],expected=404)
    invite=call(owner,'POST',path+'/invitations?role=viewer',expected=201)
    call(viewer,'POST',path+'/invitations/accept',json={'tenant_id':invite['tenant_id'],'token':invite['token']})
    viewer.headers['X-Workspace-Tenant']=invite['tenant_id']
    assert call(viewer,'GET',path+'/engineering/'+evidence['workflow_run_id'])['report']==result['report']
    assert viewer.get(BASE+result['artifacts']['engineering_field']['url']).status_code==200
    assert viewer.get(BASE+result['artifacts']['engineering_bundle']['url']).status_code==403
    call(viewer,'POST',path+'/engineering',json=payload(),expected=403)
    members_before={m['principal_id'] for m in call(owner,'GET',path+'/members')['members']}
    editor=httpx.Client(headers={'Authorization':'Bearer '+private['editor']['token']})
    invite=call(owner,'POST',path+'/invitations?role=editor',expected=201)
    call(editor,'POST',path+'/invitations/accept',json={'tenant_id':invite['tenant_id'],'token':invite['token']})
    editor.headers['X-Workspace-Tenant']=invite['tenant_id']
    new_members=[m for m in call(owner,'GET',path+'/members')['members'] if m['principal_id'] not in members_before]
    assert len(new_members)==1 and new_members[0]['role']=='editor'
    editor_id=new_members[0]['principal_id'];revoked=False
    subprocess.run([PODMAN,'stop','--time','10',WORKER],check=True,capture_output=True,timeout=30)
    try:
        cancelled=call(owner,'POST',path+'/engineering',json=payload(),expected=202)['workflow_run_id']
        call(owner,'POST','/api/tasks/'+cancelled+'/cancel',json={'reason':'Actual queued engineering cancellation acceptance'})
        recovery=call(owner,'POST',path+'/engineering',json=payload(),expected=202)['workflow_run_id']
        denied=call(editor,'POST',path+'/engineering',json=payload(),expected=202)['workflow_run_id']
        assert call(owner,'GET','/api/tasks/'+recovery+'/snapshot')['status']=='pending'
        call(owner,'DELETE',path+'/members/'+editor_id+'?expected_role=editor',expected=204);revoked=True
    finally:
        subprocess.run([PODMAN,'start',WORKER],check=True,capture_output=True,timeout=30)
        if not revoked:
            call(owner,'DELETE',path+'/members/'+editor_id+'?expected_role=editor',expected=204)
    print('worker restarted; checking queued cancellation, recovery and revoked access',flush=True)
    cancelled_task=wait_task(owner,cancelled,timeout=180)
    assert cancelled_task['status']=='cancelled' and not cancelled_task.get('artifacts'),cancelled_task
    recovered_task=wait_task(owner,recovery,timeout=420)
    assert recovered_task['status']=='succeeded',recovered_task
    denied_task=wait_task(owner,denied,timeout=180)
    assert denied_task['status']=='failed' and not denied_task.get('artifacts'),denied_task
    call(editor,'GET',path+'/engineering/'+recovery,expected=403)
    restored=call(owner,'GET',path)
    assert (restored['head_revision_id'],restored['state_version'],restored['fcstd'])==(before['head_revision_id'],before['state_version'],before['fcstd'])
    report={'cancelled_workflow':cancelled,'recovered_workflow':recovery,'revoked_workflow':denied,
        'viewer_can_inspect_results':True,'viewer_cannot_compute_or_export':True,'tenant_boundary_enforced':True,
        'queued_cancellation_persisted':True,'actual_worker_outage_recovered':True,'revocation_rechecked_before_execution':True,'CAD_head_unchanged':True}
    Path('/tmp/cad-expansion-engineering-controls.json').write_text(json.dumps(report,indent=2))
    print('CAD_ENGINEERING_CONTROLS='+json.dumps(report),flush=True)


if __name__=='__main__':main()
