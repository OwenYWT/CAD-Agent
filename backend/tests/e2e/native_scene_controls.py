"""Real queued scene cancellation, permission revocation and explicit retry."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit
from uuid import UUID

import asyncpg
import httpx


def main():
    base = os.environ['CAD_NATIVE_E2E_URL']
    worker = os.environ['CAD_NATIVE_E2E_WORKER']
    assert urlsplit(base).hostname in {'127.0.0.1','localhost'} and worker.startswith('cad-coedit-') and worker.endswith('-worker')
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    env = json.loads(Path(os.environ['CAD_NATIVE_E2E_HOST_ENV']).read_text())
    assert '/cad_coedit_live_' in env['DATABASE_URL']
    report = Path(os.environ['CAD_COEDIT_REPORT_DIR']); report.mkdir(parents=True, exist_ok=True)
    owner = httpx.Client(base_url=base, headers={'Authorization':'Bearer '+private['owner']['token']}, timeout=60)
    guest = httpx.Client(base_url=base, headers={'Authorization':'Bearer '+private['guest']['token'], 'X-Workspace-Tenant':private['tenant_id']}, timeout=60)
    path = '/api/documents/'+private['document_id']
    def call(client, method, url, expected=200, **kwargs):
        response = client.request(method,url,**kwargs)
        assert response.status_code == expected, response.text
        return response.json() if response.content else None
    def read(url): return call(owner,'GET',url)
    def wait(task_id):
        deadline = time.monotonic()+240
        while time.monotonic() < deadline:
            task = read('/api/tasks/'+task_id+'/snapshot')
            if task['status'] in {'succeeded','failed','cancelled','timed_out'}: return task
            time.sleep(1)
        raise AssertionError(task)
    before = read(path)
    operations = read(path+'/collaboration')['operations']
    history = read('/api/history/panels/'+private['panel_id']+'/snapshots')
    members = read(path+'/members')['members']
    guest_id = next(m['principal_id'] for m in members if m['role']=='viewer')
    podman = os.environ.get('CAD_NATIVE_E2E_PODMAN','/opt/homebrew/bin/podman')
    revoked = False
    subprocess.run([podman,'stop','--time','15',worker],check=True,capture_output=True)
    try:
        tasks = []
        for revision in reversed(history):
            if revision['id'] == before['head_revision_id']: continue
            if not read(path+'/revisions/'+revision['id'])['snapshot'].get('files',{}).get('fcstd'): continue
            client = owner if not tasks else guest
            response = client.get(path+'/scenes/'+revision['id']); response.raise_for_status()
            if response.status_code == 202:
                task = response.json()
                assert task['status']=='pending', task
                tasks.append(task)
            if len(tasks)==2: break
        assert len(tasks)==2, 'Need two genuinely uncached native revisions'
        cancelled, denied = tasks
        call(owner,'POST','/api/tasks/'+cancelled['workflow_run_id']+'/cancel',json={'reason':'Real queued scene cancellation acceptance'})
        call(owner,'DELETE',path+'/members/'+guest_id+'?expected_role=viewer',expected=204); revoked=True
        assert guest.get(denied['scene_url']).status_code==403
        subprocess.run([podman,'start',worker],check=True,capture_output=True)
        for task, status in ((cancelled,'cancelled'),(denied,'failed')):
            result = wait(task['workflow_run_id'])
            assert result['status']==status and not result.get('artifacts'), result
            for _ in range(3):
                observed = call(owner,'GET',task['scene_url'],expected=202)
                assert observed['workflow_run_id']==task['workflow_run_id'] and observed['status']==status
        with ThreadPoolExecutor(max_workers=10) as pool:
            retries=list(pool.map(lambda _: owner.post(cancelled['scene_url']+'/retry',json={'workflow_run_id':cancelled['workflow_run_id']}),range(10)))
        assert all(r.status_code==202 for r in retries), [r.text for r in retries]
        retry_id = retries[0].json()['workflow_run_id']
        assert retry_id != cancelled['workflow_run_id'] and all(r.json()['workflow_run_id']==retry_id for r in retries)
        result = wait(retry_id); assert result['status']=='succeeded', result
        scene=call(owner,'GET',cancelled['scene_url'])
        assert scene['revision_id']==cancelled['revision_id']
        assert all(read(path)[k]==before[k] for k in ('head_revision_id','state_version','parameter_state_sha256'))
        assert read(path+'/collaboration')['operations']==operations
        async def attempts():
            connection=await asyncpg.connect(env['DATABASE_URL'].replace('postgresql+asyncpg:','postgresql:'))
            try:
                counts={task:await connection.fetchval('SELECT count(*) FROM execution_attempts WHERE workflow_run_id=$1',UUID(task))
                    for task in (cancelled['workflow_run_id'],denied['workflow_run_id'],retry_id)}
                assert list(counts.values())==[0,0,1],counts
                return counts
            finally: await connection.close()
        facts={'cancelled_workflow':cancelled['workflow_run_id'],'revoked_workflow':denied['workflow_run_id'],
            'retry_workflow':retry_id,'attempts':asyncio.run(attempts()),'retry_requests':10,
            'terminal_get_does_not_restart':True,'head_unchanged':True,'revoked_view_denied':True}
        (report/'report.json').write_text(json.dumps(facts,indent=2));print(json.dumps(facts),flush=True)
    finally:
        subprocess.run([podman,'start',worker],check=True,capture_output=True)
        if revoked:
            invitation=call(owner,'POST',path+'/invitations?role=viewer',expected=201)
            guest.headers.pop('X-Workspace-Tenant',None)
            call(guest,'POST',path+'/invitations/accept',json={'tenant_id':invitation['tenant_id'],'token':invitation['token']})
        owner.close();guest.close()


if __name__=='__main__': main()
