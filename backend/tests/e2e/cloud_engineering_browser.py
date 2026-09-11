"""Submit, inspect and download a real solve through the browser."""
import json
import os
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright
from cloud_document_acceptance import PRIVATE, call


def main():
    private=json.loads(PRIVATE.read_text());web=os.environ['CAD_NATIVE_E2E_WEB']
    client=httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']})
    doc=call(client,'GET','/api/documents/'+private['document_id']);path='/api/documents/'+doc['document_id']
    errors,console_errors=[],[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_context(viewport={'width':1440,'height':1000}).new_page()
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('console',lambda msg:console_errors.append(msg.text) if msg.type=='error' else None)
        page.goto(web);page.locator('input[type="text"]').fill(private['owner']['phone'])
        page.locator('input[type="password"]').fill(private['owner']['password']);page.locator('button[type="submit"]').click()
        expect(page.get_by_role('button',name='新建设计',exact=True)).to_be_visible(timeout=20000)
        workspace_flow = os.getenv('CAD_ENGINEERING_WORKSPACE_FLOW') == '1'
        if workspace_flow:
            page.get_by_role('button',name='Create a rectangular plate',exact=False).first.click()
            page.get_by_role('button',name='返回项目流程：',exact=False).click()
            simulation = page.get_by_role('button',name='结构仿真',exact=True)
            expect(simulation).to_be_enabled(timeout=20000)
            simulation.click()
            panel=page.locator('section[aria-label="结构仿真工作台"]').get_by_test_id('engineering-tasks')
            expect(panel).to_be_visible()
        else:
            page.goto(web+'?document='+doc['document_id']+'&workspace='+private['tenant_id'])
            panel=page.get_by_test_id('engineering-tasks');panel.locator('summary',has_text='有限元与外轮廓加工').click()
        expect(panel.get_by_role('button',name='运行有限元计算')).to_be_disabled()
        panel.get_by_role('textbox',name='有限元材料名称').fill('Browser acceptance elastic material')
        for label,value in [('弹性模量 / MPa','210000'),('泊松比','0.3'),('网格尺寸 / mm','4'),('载荷 Z / N','1000')]:
            panel.get_by_role('spinbutton',name=label,exact=True).fill(value)
        with page.expect_response(lambda r:r.url.endswith(path+'/engineering') and r.request.method=='POST') as submitted:
            panel.get_by_role('button',name='运行有限元计算').click()
        assert submitted.value.status==202,submitted.value.text()
        task_id=submitted.value.json()['workflow_run_id']
        print('browser engineering submitted',task_id,flush=True)
        result=panel.get_by_test_id('engineering-result')
        expect(result).to_have_attribute('data-workflow',task_id,timeout=180000)
        expect(result).to_have_attribute('data-revision',doc['head_revision_id'])
        expect(result.get_by_test_id('engineering-field-viewer').locator('canvas')).to_be_visible(timeout=20000)
        stress=result.get_by_test_id('engineering-field-maximum').inner_text()
        result.get_by_role('combobox',name='有限元显示量').select_option('displacement')
        expect(result.get_by_test_id('engineering-field-maximum')).to_contain_text('mm')
        displacement=result.get_by_test_id('engineering-field-maximum').inner_text()
        result.get_by_role('slider',name='形变显示倍率').fill('25')
        expect(result.get_by_test_id('engineering-field-maximum')).to_have_text(displacement)
        with page.expect_download() as downloaded:
            result.get_by_role('button',name='下载求解证据').click()
        destination='/tmp/cad-expansion-engineering-browser-evidence.zip';downloaded.value.save_as(destination)
        assert Path(destination).stat().st_size>10000
        result.scroll_into_view_if_needed();page.screenshot(path='/tmp/cad-expansion-engineering-browser.png')
        assert not errors,errors
        assert not console_errors,console_errors
        after=call(client,'GET',path)
        assert after['head_revision_id']==doc['head_revision_id'] and after['fcstd']==doc['fcstd']
        if workspace_flow:
            page.get_by_role('button',name='返回项目流程：',exact=False).click()
            stage=page.get_by_role('heading',name='仿真验证',exact=True).locator('xpath=../../..')
            expect(stage).to_contain_text('已完成',timeout=15000)
            expect(stage).not_to_contain_text('暂未接入')
            page.screenshot(path='/tmp/cad-expansion-simulation-workspace.png')
        evidence={'workflow_run_id':task_id,'browser_submit_and_actual_solver':True,'stress':stress,'displacement':displacement,
            'actual_field_rendered':True,'display_scale_preserves_raw_values':True,'evidence_downloaded':True,
            'source_revision_unchanged':True,'main_workflow_simulation_entry_verified':workspace_flow,
            'page_errors':errors,'console_errors':console_errors}
        Path('/tmp/cad-expansion-engineering-browser.json').write_text(json.dumps(evidence,indent=2))
        print('CAD_ENGINEERING_BROWSER='+json.dumps(evidence),flush=True)
        browser.close()


if __name__=='__main__':main()
