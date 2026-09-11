"""Real 102-component assembly: two mesh downloads, native instances, WebGL."""
import json
import os
from pathlib import Path
import time

import httpx
from playwright.sync_api import expect, sync_playwright

private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
model = json.loads(Path('/tmp/cad-expansion-large-scene-document.json').read_text())
web = os.environ['CAD_NATIVE_E2E_WEB']
client = httpx.Client(base_url=os.environ['CAD_NATIVE_E2E_URL'], timeout=180,
    headers={'Authorization':'Bearer ' + private['owner']['token']})
doc = '/api/documents/' + model['document_id']
response = client.get(doc); response.raise_for_status(); snapshot = response.json()
response = client.get(doc + '/scenes/' + snapshot['head_revision_id']); response.raise_for_status(); scene = response.json()
assert len(scene['instances']) == 102 and len(scene['definitions']) == 2
assert sum(i['is_instance'] for i in scene['instances']) == 100
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
    page = browser.new_context(viewport={'width':1440,'height':1000}).new_page()
    errors, requests = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda m: errors.append(m.text) if m.type=='error' else None)
    page.on('request', lambda r: requests.append(r.url) if '/meshes/' in r.url else None)
    page.goto(web)
    page.locator('input[type="text"]').fill(private['owner']['phone'])
    page.locator('input[type="password"]').fill(private['owner']['password'])
    page.locator('button[type="submit"]').click()
    expect(page.get_by_role('button', name='新建设计', exact=True)).to_be_visible(timeout=20000)
    started = time.monotonic()
    page.goto(web + '?document=' + model['document_id'] + '&workspace=' + model['tenant_id'])
    viewer = page.get_by_test_id('document-scene')
    expect(viewer).to_have_attribute('data-revision', snapshot['head_revision_id'], timeout=30000)
    expect(viewer.get_by_text('102 个部件 · 2 份几何',exact=False)).to_be_visible()
    assert viewer.locator('canvas').evaluate("c => !!c.getContext('webgl2')")
    assert len(requests) == 2, requests
    page.wait_for_timeout(500)
    elapsed = time.monotonic() - started
    page.screenshot(path='/tmp/cad-expansion-large-scene.png')
    assert not errors, errors
    report = {'instances':102,'native_links':100,'mesh_definitions':2,'mesh_downloads':2,
        'real_webgl':True,'load_seconds':round(elapsed,2),'browser_errors':errors,'document_id':model['document_id']}
    Path('/tmp/cad-expansion-large-scene-browser.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report),flush=True)
    browser.close()
client.close()
