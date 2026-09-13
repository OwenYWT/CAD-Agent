"""Real isolated Worker outage, scene deduplication and historical browser view.

Uses actual authenticated HTTP, PostgreSQL, Temporal, FreeCAD and object bytes.
Only the explicitly named local cad-coedit Worker is stopped; it is restarted
in a finally block. Run on a document with an uncached native historical revision.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit
from uuid import UUID

import asyncpg
import httpx
from playwright.sync_api import expect, sync_playwright


def main():
    base = os.environ['CAD_NATIVE_E2E_URL']
    worker = os.environ['CAD_NATIVE_E2E_WORKER']
    podman = os.environ.get('CAD_NATIVE_E2E_PODMAN', '/opt/homebrew/bin/podman')
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    env = json.loads(Path(os.environ['CAD_NATIVE_E2E_HOST_ENV']).read_text())
    report = Path(os.environ['CAD_COEDIT_REPORT_DIR'])
    assert urlsplit(base).hostname in {'127.0.0.1', 'localhost'}
    assert worker.startswith('cad-coedit-') and worker.endswith('-worker')
    assert '/cad_coedit_live_' in env['DATABASE_URL']
    report.mkdir(parents=True, exist_ok=True)
    facts = {}

    def record(name, value):
        facts[name] = value
        (report / 'report.json').write_text(json.dumps(facts, ensure_ascii=False, indent=2))
        print(name, json.dumps(value, ensure_ascii=False), flush=True)

    owner = httpx.Client(base_url=base, headers={'Authorization': 'Bearer '+private['owner']['token']}, timeout=60)
    guest = httpx.Client(base_url=base, headers={'Authorization': 'Bearer '+private['guest']['token'],
                                               'X-Workspace-Tenant': private['tenant_id']}, timeout=60)
    doc_url = '/api/documents/'+private['document_id']

    def read(url):
        response = owner.get(url); response.raise_for_status(); return response.json()

    before = read(doc_url)
    operations = read(doc_url+'/collaboration')['operations']
    assert all(op['status'] in {'succeeded', 'committed', 'rolled_back', 'rejected', 'failed', 'cancelled', 'timed_out', 'conflicted'} for op in operations), operations
    history = read('/api/history/panels/'+private['panel_id']+'/snapshots')
    subprocess.run([podman, 'stop', '--time', '15', worker], check=True, capture_output=True)
    try:
        target = None
        for revision in reversed(history):
            if revision['id'] == before['head_revision_id']:
                continue
            view = read(doc_url+'/revisions/'+revision['id'])
            if not view['snapshot'].get('files', {}).get('fcstd'):
                continue
            response = owner.get(doc_url+'/scenes/'+revision['id'])
            response.raise_for_status()
            if response.status_code == 202:
                target, job = revision, response.json()
                break
        assert target, 'No uncached native historical revision: create a real modeling revision before rerunning'
        scene_url = doc_url+'/scenes/'+target['id']
        assert job['status'] == 'pending', job
        with ThreadPoolExecutor(max_workers=20) as pool:
            responses = list(pool.map(lambda index: (owner if index % 2 else guest).get(scene_url), range(20)))
        assert all(r.status_code == 202 and r.json()['workflow_run_id'] == job['workflow_run_id'] for r in responses), [r.text for r in responses]
        record('deduplication', {'concurrent_requests':20, 'authorized_accounts':2,
                                'workflow_id':job['workflow_run_id'], 'revision_id':target['id'], 'status':'pending'})

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            page = browser.new_page(viewport={'width':1440, 'height':960})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            try:
                page.goto(base)
                page.locator('input[type="text"]').fill(private['owner']['phone'])
                page.locator('input[type="password"]').fill(private['owner']['password'])
                page.locator('button[type="submit"]').click()
                page.get_by_role('button', name='Create a rectangular plate', exact=False).first.click()
                expect(page.get_by_test_id('view-identity')).to_have_attribute('data-revision', before['head_revision_id'], timeout=20000)
                page.get_by_role('tab', name='版本', exact=True).click()
                row = page.locator('div.rounded-lg.border.p-3').filter(has_text='修订 #'+str(target['version'])+' ·').last
                row.get_by_role('button', name='查看此版本', exact=True).click()
                expect(page.get_by_test_id('view-identity')).to_have_attribute('data-mode', 'history')
                expect(page.get_by_test_id('view-identity')).to_have_attribute('data-revision', target['id'])
                expect(page.get_by_text('场景计算已排队', exact=False)).to_be_visible(timeout=15000)
                page.screenshot(path=str(report/'01-pending-historical-scene.png'))
                subprocess.run([podman, 'start', worker], check=True, capture_output=True)
                deadline = time.monotonic()+240
                while time.monotonic() < deadline:
                    response = owner.get(scene_url); response.raise_for_status()
                    if response.status_code == 200:
                        break
                    assert response.json()['workflow_run_id'] == job['workflow_run_id']
                    assert response.json()['status'] not in {'failed','cancelled','timed_out'}, response.text
                    page.wait_for_timeout(1000)
                assert response.status_code == 200, response.text
                scene = response.json()
                expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision', target['id'], timeout=20000)
                expect(page.get_by_test_id('view-identity')).to_have_attribute('data-mode', 'history')
                assert not errors, errors
                page.screenshot(path=str(report/'02-computed-historical-scene.png'))
                record('browser', {'pending_state_visible':True, 'historical_scene_revision':target['id'], 'javascript_errors':errors})
            except Exception:
                page.screenshot(path=str(report/'failure.png'))
                raise
            finally:
                browser.close()

        meshes = []
        for definition in scene['definitions'].values():
            assert set(definition['lods']) == {'coarse','medium','fine'}
            for lod, mesh in definition['lods'].items():
                content = guest.get(mesh['url']); content.raise_for_status()
                assert len(content.content) == mesh['size_bytes']
                assert hashlib.sha256(content.content).hexdigest() == mesh['sha256']
                meshes.append({'lod':lod, 'sha256':mesh['sha256'], 'size_bytes':mesh['size_bytes']})
        times = []
        for _ in range(5):
            start = time.monotonic(); response = owner.get(scene_url)
            assert response.status_code == 200 and response.json() == scene
            times.append(round((time.monotonic()-start)*1000, 2))
        after = read(doc_url)
        assert all(after[k] == before[k] for k in ('head_revision_id','state_version','parameter_state_sha256'))
        assert read(doc_url+'/collaboration')['operations'] == operations
        assert read(doc_url+'/revisions/'+target['id'])['snapshot']['files'] == view['snapshot']['files']
        record('read_only_cache', {'head_revision_id':after['head_revision_id'], 'state_version':after['state_version'],
            'head_unchanged':True, 'model_operations_unchanged':True, 'model_file_inventory_unchanged':True,
            'cache_hit_ms':times, 'verified_meshes':meshes})

        async def evidence():
            connection = await asyncpg.connect(env['DATABASE_URL'].replace('postgresql+asyncpg:', 'postgresql:'))
            try:
                workflow = UUID(job['workflow_run_id'])
                task = await connection.fetchrow('SELECT * FROM document_scene_tasks WHERE workflow_run_id=$1', workflow)
                steps = await connection.fetch('SELECT id,status FROM step_runs WHERE workflow_run_id=$1', workflow)
                attempts = await connection.fetch('SELECT status FROM execution_attempts WHERE step_run_id=ANY($1::uuid[])', [row['id'] for row in steps])
                assert len(steps) == len(attempts) == 1 and steps[0]['status'] == attempts[0]['status'] == 'succeeded'
                assert await connection.fetchval('SELECT count(*) FROM cad_operations WHERE id=$1', workflow) == 0
                assert await connection.fetchval('SELECT count(*) FROM document_scene_tasks WHERE document_id=$1 AND revision_id=$2 AND runtime_digest=$3 AND scene_schema=$4',
                    task['document_id'], task['revision_id'], task['runtime_digest'], task['scene_schema']) == 1
                assert await connection.fetchval('SELECT status FROM workflow_runs WHERE id=$1', workflow) == 'succeeded'
                artifacts = await connection.fetch('SELECT artifact_kind,sha256,size_bytes FROM artifacts WHERE attempt_id IN (SELECT id FROM execution_attempts WHERE step_run_id=ANY($1::uuid[]))', [row['id'] for row in steps])
                assert {a['artifact_kind'] for a in artifacts} == {'scene','meshes','capability-result'}
                return {'steps':len(steps),'attempts':len(attempts), 'source_sha256':task['source_sha256'],
                        'runtime_digest':task['runtime_digest'], 'scene_schema':task['scene_schema'], 'artifacts':[dict(a) for a in artifacts]}
            finally:
                await connection.close()
        record('durable_execution', asyncio.run(evidence()))
    finally:
        subprocess.run([podman, 'start', worker], check=True, capture_output=True)
        owner.close(); guest.close()


if __name__ == '__main__':
    main()
