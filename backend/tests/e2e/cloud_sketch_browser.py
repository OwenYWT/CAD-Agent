"""Real browser local drag, typed dimensions, Agent/kernel checks and reviewed commit."""
import json
import math
import os
from pathlib import Path
from acceptance_paths import evidence_path
from uuid import uuid4

import httpx
from playwright.sync_api import expect,sync_playwright
from cloud_document_acceptance import call,wait_task
from cloud_branch_browser import review_in_browser


def main():
    private=json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    model=json.loads(Path(str(evidence_path('cad-expansion-sketch-document.json'))).read_text())
    web=os.environ['CAD_NATIVE_E2E_WEB'];path='/api/documents/'+model['document_id']
    client=httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']})
    before=call(client,'GET',path)
    other_feature=next(f for f in before['features'] if f['kernel_name']=='PadB')
    other_lease=call(client,'POST',path+'/leases',json={'feature_id':other_feature['id'],'client_id':str(uuid4()),'revision_id':before['head_revision_id']})
    operation_count=len(call(client,'GET',path+'/collaboration')['operations'])
    report={}
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_context(viewport={'width':1440,'height':1000}).new_page()
        errors,console_errors,operations,inspections=[],[],[],[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('console',lambda msg:console_errors.append(msg.text) if msg.type=='error' else None)
        page.on('request',lambda r:operations.append(r) if r.url.endswith(path+'/operations') and r.method=='POST' else None)
        page.on('request',lambda r:inspections.append(r) if r.url.endswith(path+'/inspect') else None)
        page.goto(web)
        page.locator('input[type="text"]').fill(private['owner']['phone'])
        page.locator('input[type="password"]').fill(private['owner']['password'])
        page.locator('button[type="submit"]').click()
        expect(page.get_by_role('button',name='新建设计',exact=True)).to_be_visible(timeout=20000)
        page.goto(web+'?document='+model['document_id']+'&workspace='+model['tenant_id'])
        panel=page.get_by_test_id('cloud-document-panel')
        expect(panel.get_by_role('treeitem',name='SketchA',exact=True)).to_be_visible(timeout=20000)
        panel.get_by_role('treeitem',name='SketchA',exact=True).click()
        editor=page.get_by_test_id('sketch-editor')
        circle=editor.get_by_test_id('sketch-circle-0')
        expect(circle).to_have_attribute('r','5',timeout=20000)
        expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision',before['head_revision_id'],timeout=40000)
        page.wait_for_load_state('networkidle',timeout=30000)
        handle=editor.get_by_test_id('sketch-radius-handle-0')
        handle.scroll_into_view_if_needed();box=handle.bounding_box();inspection_count=len(inspections)
        page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2)
        page.mouse.down();page.mouse.move(box['x']+box['width']/2+20,box['y']+box['height']/2,steps=50);page.mouse.up()
        radius=float(editor.get_by_role('spinbutton',name='草图约束 2 Radius').input_value())
        assert radius>5.5,radius
        assert len(operations)==0 and len(inspections)==inspection_count
        assert len(call(client,'GET',path+'/collaboration')['operations'])==operation_count
        assert call(client,'GET',path)['fcstd']['sha256']==before['fcstd']['sha256']
        for index,kind,value in [(2,'Radius','7'),(0,'DistanceX','0'),(1,'DistanceY','-3')]:
            editor.get_by_role('spinbutton',name=f'草图约束 {index} {kind}').fill(value)
        expect(circle).to_have_attribute('r','7');expect(circle).to_have_attribute('cx','0');expect(circle).to_have_attribute('cy','3')
        with page.expect_response(lambda r:r.url.endswith(path+'/operations') and r.request.method=='POST') as submitted:
            editor.get_by_role('button',name='提交草图约束',exact=True).click()
        assert submitted.value.status==202,submitted.value.text()
        assert any(l['feature_id']==other_feature['id'] for l in call(client,'GET',path+'/collaboration')['leases'])
        call(client,'DELETE',path+'/leases/'+other_lease['token'],expected=204)
        task_id=submitted.value.json()['workflow_run_id'];body=operations[0].post_data_json
        assert len(body['modification']['native_edits'])==3
        review_in_browser(page,client,task_id)
        after=call(client,'GET',path)
        expect(editor.get_by_role('button',name='读取最新草图并清除草稿',exact=True)).to_be_visible(timeout=15000)
        expect(editor.get_by_role('button',name='已提交候选计算',exact=True)).to_be_disabled()
        assert editor.get_by_role('spinbutton',name='草图约束 2 Radius').input_value()=='7'
        editor.get_by_role('button',name='读取最新草图并清除草稿',exact=True).click()
        expect(editor.get_by_test_id('sketch-preview')).to_have_attribute('data-revision',after['head_revision_id'],timeout=20000)
        expect(editor.get_by_test_id('sketch-circle-0')).to_have_attribute('r','7')
        expect(editor.get_by_test_id('sketch-circle-0')).to_have_attribute('cx','0')
        scene = page.get_by_test_id('document-scene')
        expect(scene).to_have_attribute('data-requested-revision',after['head_revision_id'])
        expect(scene).not_to_have_attribute('data-revision',before['head_revision_id'])
        expect(scene).to_have_attribute('data-revision',after['head_revision_id'],timeout=90000)
        pad=next(f for f in after['features'] if f['kernel_name']=='PadA')
        assert abs(pad['shape']['volume']-math.pi*49*10)<1e-7
        stale={**body,'idempotency_key':str(uuid4()),'lease_token':None}
        call(client,'POST',path+'/operations',expected=409,json=stale)
        replay=call(client,'POST',path+'/operations',expected=202,json=body)
        assert replay['replayed'] and replay['workflow_run_id']==task_id
        page.screenshot(path=str(evidence_path('cad-expansion-sketch-browser.png')))
        assert not errors,errors
        assert not console_errors,console_errors
        report.update(workflow_id=task_id,pointer_moves=50,drag_kernel_operations=0,drag_inspections=0,
            native_radius_mm=7,native_center_mm=[0,-3],native_volume_mm3=pad['shape']['volume'],
            stale_constraint_rejected=True,replayed_without_duplicate=True,page_errors=errors,console_errors=console_errors)
        report['independent_feature_lease_did_not_block_sketch']=True
        browser.close()
    # An impossible typed dimension must fail the real worker without rewriting
    # the user's explicit edit, producing a candidate or changing the head.
    triangle=json.loads(Path(str(evidence_path('cad-expansion-triangle-document.json'))).read_text())
    path='/api/documents/'+triangle['document_id'];base=call(client,'GET',path)
    failed=call(client,'POST',path+'/operations',expected=202,json={'action':'native.update',
        'expected_base_revision_id':base['head_revision_id'],'expected_state_version':base['state_version'],
        'idempotency_key':str(uuid4()),'modification':{'expected_state_sha256':base['parameter_state_sha256'],
            'native_edits':[{'action':'sketch.set_constraint','args':{'sketch':'SketchA','constraint_index':6,'expected_type':'Distance','value_mm':10}}]}})
    task=wait_task(client,failed['workflow_run_id'])
    assert task['status']=='failed' and not task['change_set'],{k:task.get(k) for k in ('status','error_code','error_message')}
    assert task['error_code']=='sketch_constraint_update_failed',task.get('error_message')
    events=call(client,'GET','/api/tasks/'+failed['workflow_run_id']+'/events?limit=100')
    assert not events['has_more']
    sources=[e for e in events['events'] if e['event_type'].startswith('agent.source.')]
    assert len(sources)==1 and sources[0]['payload']['generator_kind']=='structured-native-compiler-v1'
    assert sum(e['event_type']=='attempt.created' for e in events['events'])==1
    assert task['agent']['repair_count']==0
    assert not any(a['artifact_kind']=='fcstd' for a in task.get('artifacts',[]))
    assert call(client,'GET',path)['head_revision_id']==base['head_revision_id']
    assert call(client,'GET',path)['fcstd']['sha256']==base['fcstd']['sha256']
    report.update(impossible_constraint_failed_workflow=failed['workflow_run_id'],failed_edit_preserved_head=True,no_automatic_rewrite=True)
    Path(str(evidence_path('cad-expansion-sketch-browser.json'))).write_text(json.dumps(report,indent=2))
    print('local drag, native constraints, reviewed commit, stale/replay and actual solver failure passed',report,flush=True)
    client.close()


if __name__=='__main__':
    main()
