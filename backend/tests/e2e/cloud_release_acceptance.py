"""Real named release -> Temporal -> native BOM/exports -> verified S3 package."""
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4
import zipfile

import httpx
from cloud_document_acceptance import BASE, PRIVATE, call, wait_task


def main():
    private=json.loads(PRIVATE.read_text())
    client=httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']})
    path='/api/documents/'+private['document_id'];doc=call(client,'GET',path)
    tasks=call(client,'GET',path+'/engineering')['tasks']
    selected=[next(t['workflow_run_id'] for t in tasks if t['task_kind']==kind and t['status']=='succeeded'
        and t['source_revision_id']==doc['head_revision_id']) for kind in ('linear_static','contour_milling')]
    payload={'release_name':'Acceptance '+uuid4().hex[:10],'expected_revision_id':doc['head_revision_id'],
        'expected_state_version':doc['state_version'],'idempotency_key':str(uuid4()),'engineering_workflow_ids':selected}
    submission=call(client,'POST',path+'/releases',json=payload,expected=202)
    assert call(client,'POST',path+'/releases',json=payload,expected=202)['release_id']==submission['release_id']
    final=wait_task(client,submission['release_id'],timeout=420)
    assert final['status']=='succeeded',final
    result=call(client,'GET',path+'/releases/'+submission['release_id'])
    assert result['source_revision_id']==doc['head_revision_id'] and result['report']['kind']=='release_package'
    assert len(result['artifacts'])==7
    downloads={}
    for kind,ref in result['artifacts'].items():
        response=client.get(BASE+ref['url']);assert response.status_code==200,response.text
        assert len(response.content)==ref['size_bytes'] and hashlib.sha256(response.content).hexdigest()==ref['sha256']
        downloads[kind]=response.content
    manifest=json.loads(downloads['release_manifest']);bom=json.loads(downloads['release_bom_json'])
    assert manifest['source']['fcstd_sha256']==doc['fcstd']['sha256']
    assert {a['workflow_run_id'] for a in manifest['engineering_artifacts']}==set(selected)
    assert len(manifest['engineering_artifacts'])==7
    assert bom['generator']['native_type']=='Assembly::BomObject' and bom['instance_count']>0
    with zipfile.ZipFile(io.BytesIO(downloads['engineering_bundle'])) as archive:
        assert set(archive.namelist())=={'manifest.json',*(f['path'] for f in manifest['files'])}
        assert archive.read('manifest.json')==downloads['release_manifest']
        assert hashlib.sha256(archive.read('design.FCStd')).hexdigest()==doc['fcstd']['sha256']
        for item in manifest['files']:
            raw=archive.read(item['path']);assert len(raw)==item['size_bytes'] and hashlib.sha256(raw).hexdigest()==item['sha256']
    call(client,'POST',path+'/releases',json={**payload,'release_name':'changed'},expected=409)
    call(client,'POST',path+'/releases',json={**payload,'idempotency_key':str(uuid4())},expected=409)
    call(client,'POST',path+'/releases',json={**payload,'release_name':'stale','idempotency_key':str(uuid4()),
        'expected_state_version':doc['state_version']-1},expected=409)
    call(client,'POST',path+'/releases',json={**payload,'release_name':'unknown','idempotency_key':str(uuid4()),
        'engineering_workflow_ids':[str(uuid4())]},expected=422)
    guest=httpx.Client(headers={'Authorization':'Bearer '+private['guest']['token'],'X-Workspace-Tenant':private['tenant_id']})
    assert call(guest,'GET',path+'/releases/'+submission['release_id'])['source_revision_id']==doc['head_revision_id']
    call(guest,'POST',path+'/releases',json={**payload,'release_name':'viewer'},expected=403)
    assert guest.get(BASE+result['artifacts']['engineering_bundle']['url']).status_code==403
    old_evidence=Path('/tmp/cad-expansion-engineering-agent.json')
    if old_evidence.exists():
        old=json.loads(old_evidence.read_text());changed_path='/api/documents/'+old['document_id'];changed=call(client,'GET',changed_path)
        call(client,'POST',changed_path+'/releases',json={'release_name':'Reject obsolete analysis','expected_revision_id':changed['head_revision_id'],
            'expected_state_version':changed['state_version'],'idempotency_key':str(uuid4()),'engineering_workflow_ids':[old['analysis_workflow_id']]},expected=422)
    after=call(client,'GET',path)
    assert (after['head_revision_id'],after['state_version'],after['fcstd'])==(doc['head_revision_id'],doc['state_version'],doc['fcstd'])
    evidence={'document_id':doc['document_id'],'release_id':submission['release_id'],'release_name':payload['release_name'],
        'source_revision_id':doc['head_revision_id'],'native_bom':bom['generator'],'instances':bom['instance_count'],
        'engineering_workflow_ids':selected,'package_files':len(manifest['files'])+1,'all_hashes_verified':True,
        'revision_unchanged':True,'replay_and_conflicts_verified':True,'viewer_export_and_publish_denied':True,
        'previous_revision_analysis_rejected':old_evidence.exists()}
    Path('/tmp/cad-expansion-release-http.json').write_text(json.dumps(evidence,indent=2))
    Path('/tmp/cad-expansion-release-actual.zip').write_bytes(downloads['engineering_bundle'])
    print('CAD_RELEASE_HTTP='+json.dumps(evidence),flush=True)


if __name__=='__main__':main()
