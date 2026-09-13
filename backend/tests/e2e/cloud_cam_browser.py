"""Real browser CAM setup -> durable native compute -> path playback -> NC export."""
import hashlib
import json
import os
from pathlib import Path
from acceptance_paths import evidence_path
from uuid import uuid4

import httpx
from playwright.sync_api import expect,sync_playwright
from cloud_document_acceptance import PRIVATE,call,wait_task


def main():
    private=json.loads(PRIVATE.read_text());web=os.environ['CAD_NATIVE_E2E_WEB']
    client=httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']})
    path='/api/documents/'+private['document_id'];before=call(client,'GET',path)
    errors,console_errors=[],[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_context(viewport={'width':1440,'height':1000}).new_page()
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('console',lambda msg:console_errors.append(msg.text) if msg.type=='error' else None)
        page.goto(web);page.locator('input[type="text"]').fill(private['owner']['phone'])
        page.locator('input[type="password"]').fill(private['owner']['password']);page.locator('button[type="submit"]').click()
        expect(page.get_by_role('button',name='新建设计',exact=True)).to_be_visible(timeout=20000)
        page.goto(web+'?document='+before['document_id']+'&workspace='+private['tenant_id'])
        panel=page.get_by_test_id('engineering-tasks');panel.locator('summary',has_text='有限元与外轮廓加工').click()
        panel.get_by_role('combobox',name='工程计算类型').select_option('contour_milling')
        form=panel.get_by_role('form',name='外轮廓加工设置')
        expect(form.get_by_role('button',name='生成外轮廓加工路径')).to_be_disabled()
        form.get_by_role('textbox',name='刀具名称').fill('Browser acceptance 6 mm flat end mill')
        for label,value in [('刀具直径 / mm','6'),('有效刃长 / mm',os.getenv('CAD_CAM_TOOL_LENGTH_MM','30')),('每层切深 / mm','3'),('轮廓进给 / mm/min','400'),
            ('垂直进给 / mm/min','100'),('主轴转速 / rpm','12000'),('安全高度 / mm','5'),('毛坯侧边余量 / mm','5'),
            ('径向保留余量 / mm','0'),('弦差精度 / mm','0.01'),('G54 原点 X / mm','5'),('G54 原点 Y / mm','-2'),('G54 原点 Z / mm','1')]:
            form.get_by_role('spinbutton',name=label,exact=True).fill(value)
        with page.expect_response(lambda r:r.url.endswith(path+'/engineering') and r.request.method=='POST') as submitted:
            form.get_by_role('button',name='生成外轮廓加工路径').click()
        assert submitted.value.status==202,submitted.value.text()
        task_id=submitted.value.json()['workflow_run_id'];payload=submitted.value.request.post_data_json
        print('CAM browser submitted',task_id,flush=True)
        result=page.get_by_test_id('cam-result')
        expect(result).to_have_attribute('data-workflow',task_id,timeout=180000)
        expect(result).to_have_attribute('data-revision',before['head_revision_id'])
        viewer=result.get_by_test_id('cam-toolpath-viewer');expect(viewer.locator('canvas')).to_be_visible(timeout=20000)
        original=int(viewer.get_attribute('data-segments'));assert original>20
        viewer.get_by_role('slider',name='加工路径进度').fill('10');expect(viewer).to_have_attribute('data-segments','10')
        viewer.get_by_role('button',name='播放加工路径').click()
        page.wait_for_function("document.querySelector('[data-testid=cam-toolpath-viewer]').dataset.segments > 10")
        viewer.get_by_role('button',name='暂停路径播放').click()
        report=call(client,'GET',path+'/engineering/'+task_id)
        with page.expect_download() as download:
            result.get_by_role('button',name='下载 GRBL 程序').click()
        destination=Path(str(evidence_path('cad-expansion-cam-browser.nc')));download.value.save_as(destination)
        program=destination.read_bytes();ref=report['artifacts']['cam_program']
        assert len(program)==ref['size_bytes'] and hashlib.sha256(program).hexdigest()==ref['sha256']
        assert b'G21 G90 G17 G94 G40 G49 G80' in program and program.endswith(b'M5\nM2\n')
        with page.expect_download() as bundle:
            result.get_by_role('button',name='下载加工证据').click()
        bundle.value.save_as(str(evidence_path('cad-expansion-cam-browser-evidence.zip')))
        viewer.get_by_role('slider',name='加工路径进度').fill(str(original))
        result.scroll_into_view_if_needed();page.screenshot(path=str(evidence_path('cad-expansion-cam-browser.png')))
        assert not errors,errors
        assert not console_errors,console_errors
        browser.close()
    replay=call(client,'POST',path+'/engineering',json=payload,expected=202)
    assert replay['replayed'] and replay['workflow_run_id']==task_id
    invalid={**payload,'idempotency_key':str(uuid4()),'task':{**payload['task'],'tool':{**payload['task']['tool'],'cutting_length_mm':4}}}
    rejected=call(client,'POST',path+'/engineering',json=invalid,expected=202)
    failed=wait_task(client,rejected['workflow_run_id'],timeout=180)
    assert failed['status']=='failed' and failed['error_code']=='cam_tool_reach' and not failed.get('artifacts'),failed
    after=call(client,'GET',path)
    assert (after['head_revision_id'],after['state_version'],after['fcstd'])==(before['head_revision_id'],before['state_version'],before['fcstd'])
    evidence={'workflow_run_id':task_id,'failed_short_tool_workflow':rejected['workflow_run_id'],'source_revision_id':before['head_revision_id'],
        'actual_native_offsets_and_clearance':True,'path_playback_verified':True,'downloaded_nc_sha256':ref['sha256'],
        'passes':report['report']['passes'],'segments':report['report']['segments'],
        'minimum_target_clearance_mm':report['report']['minimum_target_clearance_mm'],
        'internal_loops_explicitly_excluded':report['report']['internal_loops_not_machined'],
        'idempotency_verified':True,'native_short_tool_rejected':True,'source_revision_unchanged':True,'page_errors':errors,'console_errors':console_errors}
    Path(str(evidence_path('cad-expansion-cam-browser.json'))).write_text(json.dumps(evidence,indent=2))
    print('CAD_CAM_BROWSER='+json.dumps(evidence),flush=True)


if __name__=='__main__':main()
