"""Real browser keeps its canvas and downloads only the changed component."""
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from cloud_document_acceptance import call, wait_task, commit

p = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
m = json.loads(Path('/tmp/cad-expansion-scene-document.json').read_text())
WEB = os.environ['CAD_NATIVE_E2E_WEB']
client = httpx.Client(headers={'Authorization':'Bearer ' + p['owner']['token']}, timeout=180)
doc = '/api/documents/' + m['document_id']
before = call(client, 'GET', doc)
before_scene = call(client, 'GET', doc + '/scenes/' + before['head_revision_id'])
target_length = next(p['value'] for f in before['features'] for p in f['parameters'] if p['id']=='PadB.Length') + 3
assert len(before_scene['instances']) == 3 and len(before_scene['definitions']) == 2
report = {}
def record(stage, **facts):
    report[stage] = facts
    Path('/tmp/cad-expansion-scene-browser.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(stage, facts, flush=True)

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
    page = browser.new_context(viewport={'width':1440, 'height':1000}).new_page()
    errors, console_errors, downloads = [], [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda msg: console_errors.append(msg.text) if msg.type=='error' else None)
    page.on('request', lambda r: downloads.append(r.url) if '/meshes/' in r.url else None)
    page.goto(WEB)
    page.locator('input[type="text"]').fill(p['owner']['phone'])
    page.locator('input[type="password"]').fill(p['owner']['password'])
    page.locator('button[type="submit"]').click()
    expect(page.get_by_role('button', name='新建设计', exact=True)).to_be_visible(timeout=20000)
    page.goto(WEB + '?document=' + m['document_id'] + '&workspace=' + m['tenant_id'])
    viewer = page.get_by_test_id('document-scene')
    expect(viewer).to_have_attribute('data-revision', before['head_revision_id'], timeout=30000)
    expect(viewer.get_by_text('3 个部件 · 2 份几何', exact=False)).to_be_visible()
    assert len(downloads) == 2, downloads
    canvas = viewer.locator('canvas')
    assert canvas.evaluate("c => !!c.getContext('webgl2')")
    canvas.evaluate("c => c.dataset.acceptanceIdentity = 'persistent-canvas'")
    operations_before = len(call(client, 'GET', doc + '/collaboration')['operations'])
    rect = canvas.bounding_box()
    page.mouse.move(rect['x'] + rect['width']/2, rect['y'] + rect['height']/2)
    page.mouse.down(); page.mouse.move(rect['x'] + rect['width']/2 + 70, rect['y'] + rect['height']/2 + 30, steps=8); page.mouse.up()
    page.wait_for_timeout(400)
    assert len(call(client,'GET',doc+'/collaboration')['operations']) == operations_before
    panel = page.get_by_test_id('cloud-document-panel')
    panel.get_by_role('treeitem', name='PadB', exact=True).click()
    panel.get_by_role('spinbutton', name='PadB.Length', exact=True).fill(str(target_length))
    with page.expect_response(lambda r: r.url.endswith(doc+'/operations') and r.request.method == 'POST') as submitted:
        panel.get_by_role('button', name='提交参数变更', exact=True).click()
    assert submitted.value.status == 202, submitted.value.text()
    task_id = submitted.value.json()['workflow_run_id']
    task = wait_task(client,task_id)
    assert task['status'] == 'succeeded', task.get('error_message')
    commit(client,task)
    after = call(client,'GET',doc)
    expect(viewer).to_have_attribute('data-revision',after['head_revision_id'],timeout=30000)
    assert canvas.evaluate("c => c.dataset.acceptanceIdentity") == 'persistent-canvas'
    assert len(downloads) == 3, downloads
    after_scene = call(client,'GET',doc+'/scenes/'+after['head_revision_id'])
    # Another revision in this acceptance project may already contain the
    # new length. Project-wide reuse can avoid even the changed tessellation.
    assert after_scene['generated_definitions'] <= 1 and after_scene['reused_definitions'] >= 1
    by_name = {i['kernel_name']:i for i in after_scene['instances']}
    assert by_name['BodyA']['geometry_sha256'] == by_name['InstanceA']['geometry_sha256']
    record('partial_update',three_instances_two_definitions=True,initial_mesh_downloads=2,changed_mesh_downloads=1,
        same_canvas=True,camera_created_kernel_operations=0,workflow_id=task_id)
    # Each LOD is cached independently; changing display precision never
    # modifies the model or re-executes its operation queue.
    selected = viewer.get_by_role('combobox',name='网格精度')
    for lod in ['coarse','fine','medium']:
        selected.select_option(lod)
        expect(viewer).to_have_attribute('data-lod',lod,timeout=30000)
    assert len(downloads) == 7, downloads
    assert len(call(client,'GET',doc+'/collaboration')['operations']) == operations_before + 1
    assert not errors, errors
    page.screenshot(path='/tmp/cad-expansion-component-viewer.png')
    record('lod',all_precisions_loaded=True,return_to_cached_medium_downloaded_nothing=True,operation_count_unchanged=True,page_errors=errors)
    panel.get_by_role('treeitem',name='BodyA',exact=True).click()
    instance_name='BrowserInstance'+uuid4().hex[:6]
    panel.get_by_role('textbox',name='实例名称',exact=True).fill(instance_name)
    panel.get_by_role('spinbutton',name='实例位置X',exact=True).fill('90')
    with page.expect_response(lambda r: r.url.endswith(doc+'/operations') and r.request.method=='POST') as created:
        panel.get_by_role('button',name='提交实例创建',exact=True).click()
    assert created.value.status==202,created.value.text()
    create_task=wait_task(client,created.value.json()['workflow_run_id']);commit(client,create_task)
    created_doc=call(client,'GET',doc)
    expect(viewer).to_have_attribute('data-revision',created_doc['head_revision_id'],timeout=30000)
    created_scene=call(client,'GET',doc+'/scenes/'+created_doc['head_revision_id'])
    assert len(created_scene['instances'])==4 and created_scene['generated_definitions']==0
    assert len(downloads)==7,downloads
    panel.get_by_role('treeitem',name=instance_name,exact=True).click()
    expect(panel.get_by_role('spinbutton',name='实例位置X',exact=True)).to_have_value('90')
    panel.get_by_role('spinbutton',name='实例位置X',exact=True).fill('100')
    with page.expect_response(lambda r: r.url.endswith(doc+'/operations') and r.request.method=='POST') as moved:
        panel.get_by_role('button',name='提交实例位置',exact=True).click()
    assert moved.value.status==202,moved.value.text()
    move_task=wait_task(client,moved.value.json()['workflow_run_id']);commit(client,move_task)
    final_doc=call(client,'GET',doc)
    expect(viewer).to_have_attribute('data-revision',final_doc['head_revision_id'],timeout=30000)
    final_scene=call(client,'GET',doc+'/scenes/'+final_doc['head_revision_id'])
    assert next(i for i in final_scene['instances'] if i['kernel_name']==instance_name)['matrix'][3]==100
    assert final_scene['generated_definitions']==0 and len(downloads)==7,downloads
    assert canvas.evaluate("c => c.dataset.acceptanceIdentity")=='persistent-canvas'
    assert not errors,errors
    assert not console_errors,console_errors
    page.screenshot(path='/tmp/cad-expansion-component-instances.png')
    record('native_instances',created_in_browser=True,moved_in_browser=True,geometry_downloads_added=0,
        source_geometry_unchanged=True,final_instance_x_mm=100,page_errors=errors,console_errors=console_errors)
    browser.close()
client.close()
