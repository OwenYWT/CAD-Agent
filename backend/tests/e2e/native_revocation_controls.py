"""Real queued native edit and artifact/review boundaries after revocation."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg
import httpx

from acceptance_paths import evidence_path
from cloud_document_acceptance import BASE, PRIVATE, call, wait_task


def main():
    worker = os.environ['CAD_NATIVE_E2E_WORKER']
    assert urlsplit(BASE).hostname in {'localhost', '127.0.0.1'}
    assert worker.startswith('cad-coedit-') and worker.endswith('-worker')
    private = json.loads(PRIVATE.read_text())
    env = json.loads(Path(os.environ['CAD_NATIVE_E2E_HOST_ENV']).read_text())
    assert '/cad_coedit_live_' in env['DATABASE_URL']
    fixture = json.loads(evidence_path('cad-expansion-collaboration-document.json').read_text())
    path = '/api/documents/' + fixture['document_id']
    owner = httpx.Client(headers={'Authorization':'Bearer ' + private['owner']['token']})
    editor = httpx.Client(headers={'Authorization':'Bearer ' + private['editor']['token']})
    invitation = call(owner, 'POST', path + '/invitations?role=editor', expected=201)
    call(editor, 'POST', path + '/invitations/accept', json={
        'tenant_id':invitation['tenant_id'], 'token':invitation['token']})
    editor.headers['X-Workspace-Tenant'] = invitation['tenant_id']
    members = call(owner, 'GET', path + '/members')['members']
    editors = [m for m in members if m['role'] == 'editor']
    assert len(editors) == 1
    editor_id = editors[0]['principal_id']
    before = call(owner, 'GET', path)
    current = next(p['value'] for f in before['features'] for p in f['parameters'] if p['id'] == 'PadA.Length')
    def payload(value):
        return {'action':'parameters.update', 'expected_base_revision_id':before['head_revision_id'],
            'expected_state_version':before['state_version'], 'allow_rebase':False,
            'idempotency_key':str(uuid4()), 'modification':{
                'expected_state_sha256':before['parameter_state_sha256'],
                'parameter_updates':[{'parameter_id':'PadA.Length', 'value':value}]}}
    # Keep a genuine accepted, uncommitted candidate for the post-revocation
    # review/commit checks. Editors can review but never own the commit grant.
    candidate = call(editor, 'POST', path + '/operations', expected=202, json=payload(current + 1))
    solved = wait_task(owner, candidate['workflow_run_id'])
    assert solved['status'] == 'succeeded', solved.get('error_message')
    change = solved['change_set']['id']
    call(editor, 'POST', '/api/change-sets/' + change + '/accept', json={'note':'Actual candidate before access revocation'})
    view = call(owner, 'GET', path + '/revisions/' + solved['change_set']['candidate_revision_id'])
    download = view['snapshot']['files']['fcstd']
    assert editor.get(BASE + download, timeout=60).status_code == 200
    assert call(editor, 'GET', path)['can_commit'] is False
    podman = os.getenv('CAD_NATIVE_E2E_PODMAN', '/opt/homebrew/bin/podman')
    stopped = False
    try:
        subprocess.run([podman, 'stop', '--time', '15', worker], check=True, capture_output=True)
        stopped = True
        queued = call(editor, 'POST', path + '/operations', expected=202, json=payload(current + 2))
        call(owner, 'DELETE', path + '/members/' + editor_id + '?expected_role=editor', expected=204)
        assert editor.get(BASE + download, timeout=60).status_code == 403
        call(editor, 'POST', '/api/change-sets/' + change + '/accept', expected=403, json={'note':'Revoked review must fail'})
        call(editor, 'POST', '/api/change-sets/' + change + '/commit', expected=403)
    finally:
        subprocess.run([podman, 'start', worker], check=True, capture_output=True)
    assert stopped
    denied = wait_task(owner, queued['workflow_run_id'], timeout=240)
    assert denied['status'] == 'failed' and not denied.get('artifacts') and not denied.get('change_set'), denied
    after = call(owner, 'GET', path)
    assert all(after[k] == before[k] for k in ('head_revision_id', 'state_version', 'parameter_state_sha256'))
    async def verify_database():
        connection = await asyncpg.connect(env['DATABASE_URL'].replace('postgresql+asyncpg:', 'postgresql:'))
        try:
            assert await connection.fetchval('SELECT count(*) FROM execution_attempts WHERE workflow_run_id=$1', UUID(queued['workflow_run_id'])) == 0
            assert await connection.fetchval('SELECT status FROM change_sets WHERE id=$1', UUID(change)) == 'accepted'
            assert await connection.fetchval('SELECT count(*) FROM document_feature_leases WHERE document_id=$1 AND principal_id=$2', UUID(fixture['document_id']), UUID(editor_id)) == 0
        finally:
            await connection.close()
    asyncio.run(verify_database())
    report = {'status':'passed', 'candidate_workflow':candidate['workflow_run_id'], 'accepted_change_set':change,
        'queued_workflow':queued['workflow_run_id'], 'queued_native_attempts':0,
        'queued_revoked_user_cannot_execute':True, 'previously_readable_candidate_download_denied':True,
        'revoked_review_and_commit_denied':True, 'editor_commit_grant_was_already_absent':True,
        'accepted_candidate_preserved':True, 'head_unchanged':True}
    evidence_path('native-revocation-controls.json').write_text(json.dumps(report, indent=2))
    print('CAD_NATIVE_REVOCATION=' + json.dumps(report), flush=True)
    owner.close()
    editor.close()


if __name__ == '__main__':
    main()
