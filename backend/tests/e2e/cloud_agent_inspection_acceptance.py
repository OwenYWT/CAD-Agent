"""Real HTTP/Temporal provider inspection loop -> native fillet -> reviewed commit."""
import hashlib
import json
import os
from pathlib import Path
from acceptance_paths import evidence_path
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright

from cloud_document_acceptance import BASE, PRIVATE, call, commit, wait_task


def verify_browser(private, document, fillet):
    errors=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_context(viewport={'width':1440,'height':1000}).new_page()
        page.on('pageerror',lambda error:errors.append(str(error)))
        web=os.environ['CAD_NATIVE_E2E_WEB'];page.goto(web)
        page.locator('input[type="text"]').fill(private['owner']['phone'])
        page.locator('input[type="password"]').fill(private['owner']['password'])
        page.locator('button[type="submit"]').click()
        expect(page.get_by_role('button',name='新建设计',exact=True)).to_be_visible(timeout=20000)
        page.goto(web+'?document='+document['document_id']+'&workspace='+private['tenant_id'])
        expect(page.get_by_role('treeitem',name=fillet['label'],exact=True)).to_be_visible(timeout=20000)
        viewer=page.get_by_test_id('document-scene')
        expect(viewer).to_have_attribute('data-revision',document['head_revision_id'],timeout=60000)
        expect(viewer.get_by_text('1 个部件 · 1 份几何',exact=False)).to_be_visible()
        assert viewer.locator('canvas').evaluate("c => !!c.getContext('webgl2')")
        page.wait_for_timeout(500)
        page.screenshot(path=str(evidence_path('cad-expansion-agent-inspection-browser.png')))
        assert not errors,errors
        browser.close()
    return errors


def main():
    private=json.loads(PRIVATE.read_text())
    client=httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']})
    source=call(client,'GET','/api/documents/'+private['document_id'])
    fork=call(client,'POST','/api/documents/'+source['document_id']+'/branches',expected=202,json={
        'name':'provider-inspection-'+uuid4().hex[:8], 'expected_revision_id':source['head_revision_id'],
        'expected_state_version':source['state_version'],'idempotency_key':str(uuid4())})
    commit(client,wait_task(client,fork['workflow_run_id']))
    path='/api/documents/'+fork['document_id'];before=call(client,'GET',path)
    if os.getenv('CAD_INSPECTION_FIXTURE_THICKNESS'):
        from cloud_merge_acceptance import modify
        pad=next(f for f in before['features'] if f['type']=='PartDesign::Pad')
        length=next(p for p in pad['parameters'] if p['property_name']=='Length')
        before=modify(client,fork['document_id'],length['id'],float(os.environ['CAD_INSPECTION_FIXTURE_THICKNESS']))
    body=next(f for f in before['features'] if f['type']=='PartDesign::Body')
    operation=call(client,'POST',path+'/operations',expected=202,json={
        'action':'modify','expected_base_revision_id':before['head_revision_id'],
        'expected_state_version':before['state_version'],'idempotency_key':str(uuid4()),
        'objective':f"Inspect {body['kernel_name']} and its actual final feature and topology before making a plan. Add exactly one native PartDesign fillet of radius 0.5 mm to all edges of that final feature, using feature.fillet with use_all_edges=true. Preserve the existing plate dimensions, through-hole diameter and all prior feature names. Keep the existing feature history, and export STEP and STL."})
    task=wait_task(client,operation['workflow_run_id']);commit(client,task)
    after=call(client,'GET',path)
    assert after['head_revision_id']!=before['head_revision_id']
    assert {f['id'] for f in before['features']} <= {f['id'] for f in after['features']}
    previous={p['id']:p['value'] for f in before['features'] for p in f['parameters']}
    current={p['id']:p['value'] for f in after['features'] for p in f['parameters']}
    assert all(current[key]==value for key,value in previous.items())
    fillet=next(f for f in after['features'] if f['type']=='PartDesign::Fillet')
    assert next(p['value'] for p in fillet['parameters'] if p['property_name']=='Radius')==0.5
    assert after['fcstd']['sha256']!=before['fcstd']['sha256']
    events=[];cursor=0
    for _ in range(10):
        page=call(client,'GET',f"/api/tasks/{operation['workflow_run_id']}/events?after_sequence={cursor}&limit=500")
        events.extend(page['events']);cursor=page['next_cursor']
        if not page['has_more']:break
    else:raise AssertionError('unexpected unbounded task event history')
    inspections=[event['payload'] for event in events if event['event_type']=='agent.kernel_inspected'
                 and event['payload'].get('provider',{}).get('provider_response_id')]
    assert inspections,'workflow did not persist a real provider-requested kernel query'
    assert all(i['result']['source']=='verified_kernel_checkpoint' and i['provider']['provider']!='deterministic' for i in inspections)
    for kind in ('fcstd','state','mesh'):
        ref=after[kind];response=client.get(BASE+ref['url']);assert response.status_code==200
        assert hashlib.sha256(response.content).hexdigest()==ref['sha256']
    errors=verify_browser(private,after,fillet)
    evidence={'document_id':fork['document_id'],'workflow_run_id':operation['workflow_run_id'],
              'source_revision_id':before['head_revision_id'],'committed_revision_id':after['head_revision_id'],
              'fillet_radius_mm':0.5,'previous_parameters_preserved':True,'previous_feature_ids_preserved':True,
              'real_provider_inspection_events':inspections,'native_artifact_hashes_verified':True,
              'normal_review_and_commit':True,'committed_feature_rendered_in_browser':True,
              'rendered_scene_revision':after['head_revision_id'],'page_errors':errors}
    Path(str(evidence_path('cad-expansion-agent-inspection-http.json'))).write_text(json.dumps(evidence,indent=2))
    print('CAD_AGENT_INSPECTION_HTTP='+json.dumps({key:value for key,value in evidence.items()
          if key!='real_provider_inspection_events'}),flush=True)


if __name__=='__main__':
    main()
