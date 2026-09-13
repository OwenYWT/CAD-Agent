"""Real HTTP -> outbox -> Temporal -> native solver -> S3 -> revision evidence."""
import hashlib
import io
import json
import os
from pathlib import Path
from acceptance_paths import evidence_path
from uuid import uuid4
import zipfile

import httpx

from cloud_document_acceptance import BASE, PRIVATE, call, wait_task


def main():
    private = json.loads(PRIVATE.read_text())
    client = httpx.Client(headers={'Authorization': 'Bearer '+private['owner']['token']})
    doc_id = os.getenv('CAD_ENGINEERING_DOCUMENT', private['document_id'])
    path = '/api/documents/'+doc_id
    before = call(client, 'GET', path)
    component = next(f['kernel_name'] for f in before['features'] if f['type'] == 'PartDesign::Body')
    payload = {'expected_revision_id': before['head_revision_id'], 'expected_state_version': before['state_version'],
        'idempotency_key': str(uuid4()), 'task': {'kind': 'linear_static', 'component_name': component,
            'material': {'name': 'Acceptance elastic material', 'young_modulus_mpa': 210000, 'poisson_ratio': 0.3},
            'mesh_size_mm': 4, 'fixed_face': {'axis': 'z', 'side': 'min'}, 'loaded_face': {'axis': 'z', 'side': 'max'}, 'force_n': [0, 0, 1000]}}
    submitted = call(client, 'POST', path+'/engineering', json=payload, expected=202)
    assert call(client, 'POST', path+'/engineering', json=payload, expected=202)['workflow_run_id'] == submitted['workflow_run_id']
    task = wait_task(client, submitted['workflow_run_id'], timeout=420)
    assert task['status'] == 'succeeded', task
    assert not task.get('change_set')
    result = call(client, 'GET', path+'/engineering/'+submitted['workflow_run_id'])
    assert result['source_revision_id'] == before['head_revision_id']
    report = result['report']
    assert report['source_fcstd_sha256'] == before['fcstd']['sha256']
    assert report['maximum']['displacement_mm'] > 0 and report['maximum']['von_mises_mpa'] > 0
    assert report['force_balance_relative_error'] < 0.001
    downloads = {}
    for kind, ref in result['artifacts'].items():
        response = client.get(BASE+ref['url']); assert response.status_code == 200
        assert len(response.content) == ref['size_bytes'] and hashlib.sha256(response.content).hexdigest() == ref['sha256']
        downloads[kind] = response.content
    with zipfile.ZipFile(io.BytesIO(downloads['engineering_bundle'])) as archive:
        assert {'analysis.inp', 'analysis.frd', 'analysis.dat', 'calculix.log', 'component.step'} <= set(archive.namelist())
    field = json.loads(downloads['engineering_field'])
    assert len(field['positions_mm']) == report['nodes'] and field['triangles']
    after = call(client, 'GET', path)
    assert (after['head_revision_id'], after['state_version'], after['fcstd']) == (before['head_revision_id'], before['state_version'], before['fcstd'])
    assert not any(o['id'] == submitted['workflow_run_id'] for o in after.get('operations', []))
    for bad in ({**payload, 'task': {**payload['task'], 'force_n': [0, 0, 0]}},
                {**payload, 'task': {**payload['task'], 'material': {'name': 'Unknown'}}}):
        call(client, 'POST', path+'/engineering', json=bad, expected=422)
    call(client, 'POST', path+'/engineering', json={**payload, 'task': {**payload['task'], 'mesh_size_mm': 5}}, expected=409)
    call(client, 'POST', path+'/engineering', json={**payload, 'idempotency_key': str(uuid4()), 'expected_state_version': before['state_version']-1}, expected=409)
    invalid = call(client, 'POST', path+'/engineering', json={**payload, 'idempotency_key': str(uuid4()),
        'task': {**payload['task'], 'component_name': 'MissingComponent'}}, expected=202)
    failed = wait_task(client, invalid['workflow_run_id'], timeout=420)
    assert failed['status'] == 'failed' and not failed.get('artifacts'), failed
    call(client, 'GET', path+'/engineering/'+invalid['workflow_run_id'], expected=409)
    evidence = {'document_id': doc_id, 'workflow_run_id': submitted['workflow_run_id'], 'failed_workflow_run_id': invalid['workflow_run_id'],
        'source_revision_id': result['source_revision_id'], 'actual_solver': report['solver'], 'nodes': report['nodes'],
        'maximum': report['maximum'], 'force_balance_relative_error': report['force_balance_relative_error'],
        'idempotency_verified': True, 'invalid_requests_rejected': True, 'actual_missing_component_failed': True,
        'revision_unchanged': True, 'all_artifact_hashes_verified': True}
    Path(str(evidence_path('cad-expansion-engineering-http.json'))).write_text(json.dumps(evidence, indent=2))
    print('CAD_ENGINEERING_HTTP='+json.dumps(evidence), flush=True)


if __name__ == '__main__':
    main()
