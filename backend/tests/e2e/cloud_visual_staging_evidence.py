"""Download both real visual-validation stages, including blocked output.

This runs read-only provenance queries inside the isolated API container and
verifies S3 bytes. It does not publish failed artifacts or modify workflow state.
CAD_VISUAL_STAGE_REPORT_DIR names a completed visual browser test directory.
"""
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

from cloud_document_acceptance import PRIVATE


SCRIPT = r'''
import asyncio, hashlib, json, sys
from pathlib import Path
from uuid import UUID
from sqlalchemy import text
from botocore.exceptions import ClientError
from app.db import close_database, tenant_transaction
from app.domain.identity import user_principal
from app.object_store import get_object

async def main():
    owner = user_principal(sys.argv[1])
    workflow_id = UUID(sys.argv[2])
    target = Path(sys.argv[3])
    target.mkdir(exist_ok=False)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        manifests = (await conn.execute(text("SELECT id, manifest, supersedes_id FROM agent_staging_manifests WHERE workflow_run_id=:id ORDER BY created_at, id"), {'id':workflow_id})).mappings().all()
        sources = (await conn.execute(text("SELECT id, predecessor_source_id, source_hash, source_code, generator_kind, provider, model, provider_response_id, request_hash, response_hash FROM agent_generated_sources WHERE workflow_run_id=:id ORDER BY created_at, id"), {'id':workflow_id})).mappings().all()
        visual = (await conn.execute(text("SELECT id, staging_manifest_id, outcome, evidence FROM agent_validation_evidence WHERE workflow_run_id=:id AND gate='visual' ORDER BY created_at, id"), {'id':workflow_id})).mappings().all()
        artifacts = (await conn.execute(text("SELECT artifact_kind, sha256, size_bytes, object_key FROM artifacts WHERE workflow_run_id=:id"), {'id':workflow_id})).mappings().all()
    assert len(manifests) == 2 and len(visual) == 2 and len(sources) == 2, (len(manifests),len(visual),len(sources))
    assert sources[1]['predecessor_source_id'] == sources[0]['id']
    assert manifests[1]['supersedes_id'] == manifests[0]['id']
    source_by_id = {str(s['id']): s for s in sources}
    stages = []
    for index, row in enumerate(manifests):
        manifest = row['manifest']
        source = source_by_id[manifest['source_id']]
        assert hashlib.sha256(source['source_code'].encode()).hexdigest() == source['source_hash'] == manifest['source_hash']
        assert source['provider'] not in {None, 'mock', 'controlled-repair-provider'}
        if index:
            assert source['provider'] != 'deterministic' and source['provider_response_id']
        (target / f'{index}-operations.json').write_text(source['source_code'])
        checked = {}
        for output in manifest['outputs']:
            if output['format'] not in {'fcstd', 'step', 'state'}:
                continue
            location = 'staging'
            try:
                payload = await get_object(output['object_key'])
            except ClientError as exc:
                if exc.response['Error']['Code'] != 'NoSuchKey':
                    raise
                # Sealing promotes selected output then removes its staging key.
                # Resolve only this workflow's exact hash/kind/length; no regeneration.
                matches = [a for a in artifacts if a['sha256']==output['sha256']
                    and a['artifact_kind']==output['format'] and a['size_bytes']==output['size_bytes']]
                assert len(matches)==1, (index,output['format'],len(matches))
                payload = await get_object(matches[0]['object_key'])
                location = 'promoted_artifact'
            assert len(payload) == output['size_bytes']
            assert hashlib.sha256(payload).hexdigest() == output['sha256']
            suffix = 'json' if output['format'] == 'state' else output['format']
            path = target / f'{index}-model.{suffix}'
            path.write_bytes(payload)
            checked[output['format']] = {'filename':path.name, 'sha256':output['sha256'], 'size_bytes':len(payload), 'storage':location}
        assert {'fcstd','step','state'} <= checked.keys()
        evidence = next(v for v in visual if v['staging_manifest_id'] == row['id'])
        assert evidence['evidence']['provider_provenance']['provider_response_id']
        stages.append({'index':index, 'manifest_id':str(row['id']),
            'source':{k:str(v) if k in {'id','predecessor_source_id'} and v is not None else v for k,v in source.items() if k!='source_code'},
            'visual_outcome':evidence['outcome'], 'visual_evidence':evidence['evidence'], 'verified_files':checked})
    (target/'provenance.json').write_text(json.dumps({'workflow_id':str(workflow_id),'stages':stages},ensure_ascii=False,indent=2))
    print('CAD_VISUAL_STAGES_VERIFIED='+json.dumps({'workflow_id':str(workflow_id),'stages':len(stages),'outcomes':[s['visual_outcome'] for s in stages]}),flush=True)
    await close_database()
asyncio.run(main())
'''


def main():
    out = Path(os.environ['CAD_VISUAL_STAGE_REPORT_DIR'])
    task = json.loads((out / 'task.json').read_text())
    private = json.loads(PRIVATE.read_text())
    destination = out / 'staging'
    assert not destination.exists(), 'use a new evidence directory; preserve prior measurements'
    runtime = os.getenv('CAD_PODMAN', '/opt/homebrew/bin/podman')
    api = os.environ['CAD_NATIVE_E2E_API']
    remote = '/tmp/cad-visual-stage-' + task['id'] + '-' + uuid4().hex[:8]
    result = subprocess.run([runtime,'exec','-i',api,'python','-',
        private['owner']['user']['id'], task['id'], remote],
        input=SCRIPT,text=True,capture_output=True,timeout=120)
    log = out / 'staging-download.log'
    if log.exists():
        log = out / ('staging-download-' + uuid4().hex[:8] + '.log')
    log.write_text(result.stdout+result.stderr)
    assert result.returncode == 0, (result.stdout+result.stderr)[-2500:]
    subprocess.run([runtime,'cp',api+':'+remote,str(destination)],check=True,timeout=60)
    print(result.stdout.strip(),flush=True)


if __name__ == '__main__':
    main()
