"""Browser publishes the real revision, renders native BOM and downloads a verified bundle."""
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
from playwright.sync_api import expect,sync_playwright
from cloud_document_acceptance import PRIVATE,call


def main():
    private=json.loads(PRIVATE.read_text());web=os.environ['CAD_NATIVE_E2E_WEB']
    client=httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']})
    doc=call(client,'GET','/api/documents/'+private['document_id']);path='/api/documents/'+doc['document_id']
    prior=json.loads(Path('/tmp/cad-expansion-release-http.json').read_text())
    errors=[];console_errors=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_context(viewport={'width':1440,'height':1000}).new_page()
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('console',lambda m:console_errors.append(m.text) if m.type=='error' else None)
        page.goto(web);page.locator('input[type="text"]').fill(private['owner']['phone'])
        page.locator('input[type="password"]').fill(private['owner']['password']);page.locator('button[type="submit"]').click()
        expect(page.get_by_role('button',name='新建设计',exact=True)).to_be_visible(timeout=20000)
        page.goto(web+'?document='+doc['document_id']+'&workspace='+private['tenant_id'])
        panel=page.get_by_test_id('document-releases');panel.locator('summary',has_text='版本发布与 BOM').click()
        expect(panel.get_by_role('button',name='创建不可变发布')).to_be_disabled()
        panel.get_by_role('textbox',name='发布名称').fill('Browser '+uuid4().hex[:10])
        for analysis in prior['engineering_workflow_ids']:panel.locator(f'input[data-evidence="{analysis}"]').check()
        with page.expect_response(lambda r:r.url.endswith(path+'/releases') and r.request.method=='POST') as submitted:
            panel.get_by_role('button',name='创建不可变发布').click()
        assert submitted.value.status==202,submitted.value.text()
        release=submitted.value.json()['release_id'];result=panel.get_by_test_id('release-result')
        expect(result).to_have_attribute('data-release',release,timeout=180000)
        expect(result.get_by_role('table',name='发布 BOM')).to_be_visible()
        expect(result).to_contain_text('附带 2 项同修订工程任务证据')
        with page.expect_download() as download:result.get_by_role('button',name='下载完整发布包').click()
        destination=Path('/tmp/cad-expansion-release-browser.zip');download.value.save_as(destination)
        reference=call(client,'GET',path+'/releases/'+release)['artifacts']['engineering_bundle']
        assert hashlib.sha256(destination.read_bytes()).hexdigest()==reference['sha256']
        result.scroll_into_view_if_needed();page.screenshot(path='/tmp/cad-expansion-release-browser.png')
        after=call(client,'GET',path)
        assert (after['head_revision_id'],after['state_version'],after['fcstd'])==(doc['head_revision_id'],doc['state_version'],doc['fcstd'])
        assert not errors and not console_errors,(errors,console_errors)
        evidence={'release_id':release,'actual_native_bom_rendered':True,'selected_engineering_tasks':prior['engineering_workflow_ids'],
            'bundle_hash_verified':True,'revision_unchanged':True,'page_errors':errors,'console_errors':console_errors}
        Path('/tmp/cad-expansion-release-browser.json').write_text(json.dumps(evidence,indent=2))
        print('CAD_RELEASE_BROWSER='+json.dumps(evidence),flush=True);browser.close()


if __name__=='__main__':main()
