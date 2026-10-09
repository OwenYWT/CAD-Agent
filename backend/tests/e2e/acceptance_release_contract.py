"""Real browser publication, selected native evidence, package and role checks."""
import hashlib
import io
import json
from uuid import uuid4
import zipfile

import httpx
from playwright.sync_api import expect


def verify_release(page, api, private, fixture, out):
    path = '/api/documents/' + fixture['document_id']
    before = api.get(path).raise_for_status().json()
    measurement = json.loads((out / 'native-volume.json').read_text())
    evidence_id = measurement['workflow_run_id']
    page.get_by_role('button', name='版本', exact=True).click()
    page.locator('.ww-inspector-pane').locator('summary', has_text='分支与发布').click()
    panel = page.get_by_test_id('document-releases')
    panel.locator('summary', has_text='版本发布与 BOM').click()
    panel.get_by_role('textbox', name='发布名称', exact=True).fill('Verified release ' + uuid4().hex[:8])
    panel.locator(f'input[data-evidence="{evidence_id}"]').check()
    with page.expect_response(lambda r: r.url.endswith(path + '/releases') and r.request.method == 'POST') as submitted:
        panel.get_by_role('button', name='创建不可变发布', exact=True).click()
    assert submitted.value.status == 202, submitted.value.text()
    payload = submitted.value.request.post_data_json
    release_id = submitted.value.json()['release_id']
    result = panel.get_by_test_id('release-result')
    expect(result).to_have_attribute('data-release', release_id, timeout=180000)
    expect(result.get_by_role('table', name='发布 BOM')).to_be_visible()
    expect(result).to_contain_text('附带 1 项同修订工程任务证据')
    published = api.get(path + '/releases/' + release_id).raise_for_status().json()
    downloads = {}
    for kind, ref in published['artifacts'].items():
        raw = api.get(ref['url']).raise_for_status().content
        assert len(raw) == ref['size_bytes'] and hashlib.sha256(raw).hexdigest() == ref['sha256']
        downloads[kind] = raw
    manifest = json.loads(downloads['release_manifest'])
    assert manifest['source']['fcstd_sha256'] == before['fcstd']['sha256']
    assert {item['workflow_run_id'] for item in manifest['engineering_artifacts']} == {evidence_id}
    with zipfile.ZipFile(io.BytesIO(downloads['engineering_bundle'])) as archive:
        assert set(archive.namelist()) == {'manifest.json', *(item['path'] for item in manifest['files'])}
        assert hashlib.sha256(archive.read('design.FCStd')).hexdigest() == before['fcstd']['sha256']
        for item in manifest['files']:
            raw = archive.read(item['path'])
            assert len(raw) == item['size_bytes'] and hashlib.sha256(raw).hexdigest() == item['sha256']
    with page.expect_download() as download:
        result.get_by_role('button', name='下载完整发布包', exact=True).click()
    downloaded = out / 'release-browser.zip'
    download.value.save_as(downloaded)
    assert downloaded.read_bytes() == downloads['engineering_bundle']
    assert api.post(path + '/releases', json=payload).raise_for_status().json()['release_id'] == release_id
    for changed, status in [
        ({'release_name': 'Changed'}, 409),
        ({'idempotency_key': str(uuid4())}, 409),
        ({'release_name': 'Stale', 'idempotency_key': str(uuid4()), 'expected_state_version': before['state_version'] - 1}, 409),
        ({'release_name': 'Missing evidence', 'idempotency_key': str(uuid4()), 'engineering_workflow_ids': [str(uuid4())]}, 422),
    ]:
        assert api.post(path + '/releases', json={**payload, **changed}).status_code == status
    with httpx.Client(base_url=api.base_url, timeout=60, trust_env=False,
            headers={'Authorization': 'Bearer ' + private['editor']['token']}) as guest:
        assert guest.get(path + '/releases/' + release_id).status_code in {403, 404}
        invitation = api.post(path + '/invitations?role=viewer').raise_for_status().json()
        accepted = guest.post(path + '/invitations/accept', json={key: invitation[key] for key in ('tenant_id', 'token')}).raise_for_status().json()
        guest.headers['X-Workspace-Tenant'] = invitation['tenant_id']
        assert accepted['role'] == 'viewer'
        assert guest.get(path + '/releases/' + release_id).status_code == 200
        assert guest.post(path + '/releases', json={**payload, 'idempotency_key': str(uuid4())}).status_code == 403
        bundle = published['artifacts']['engineering_bundle']['url']
        assert guest.get(bundle).status_code == 403
        members = api.get(path + '/members').raise_for_status().json()['members']
        member = next(item for item in members if item['role'] == 'viewer')
        api.patch(path + '/members/' + member['principal_id'], json={'expected_role': 'viewer', 'role': 'editor'}).raise_for_status()
        assert guest.get(bundle).raise_for_status().content == downloads['engineering_bundle']
        assert guest.post(path + '/releases', json={**payload, 'idempotency_key': str(uuid4())}).status_code == 403
        api.delete(path + '/members/' + member['principal_id'] + '?expected_role=editor').raise_for_status()
        assert guest.get(bundle).status_code == 403
    assert httpx.get(str(api.base_url).rstrip('/') + bundle, trust_env=False).status_code == 401
    after = api.get(path).raise_for_status().json()
    assert (after['head_revision_id'], after['state_version'], after['fcstd']) == (before['head_revision_id'], before['state_version'], before['fcstd'])
    page.screenshot(path=str(out / 'release-browser.png'))
    (out / 'release.json').write_text(json.dumps({'passed': True, 'classification': 'REAL_BROWSER_API_TEMPORAL_FREECAD',
        'release_id': release_id, 'evidence_id': evidence_id, 'package_hash_verified': True,
        'roles': ['owner', 'editor', 'viewer', 'outsider', 'revoked-editor', 'anonymous'],
        'idempotency_conflicts_and_unknown_evidence': True, 'head_unchanged': True}, indent=2))
