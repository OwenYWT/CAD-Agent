"""A genuine first native candidate is not an editable committed model.

The fixture uses the real deterministic FreeCAD compiler, Temporal, sandbox and
S3. One outgoing browser prompt is held before delivery to inspect its intent;
no response, Provider output or successful workflow result is substituted.
The same candidate is then accepted/committed through the real browser.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright


SEED = '''import asyncio,json,sys
from app.domain.identity import user_principal
from app.services.durable_submission import ensure_workspace_identity
from app.freecad.operation_compiler import compile_common_generation
from app.workflows.temporal import start_mcad_workflow,McadExecutionRequest,McadOutputRequest
from app.db import close_database
async def main():
    owner=user_principal(sys.argv[1])
    workspace=await ensure_workspace_identity(owner,session_id=sys.argv[2],panel_id=sys.argv[3],title=sys.argv[4],user_id=sys.argv[1])
    plan=compile_common_generation({'part_type':'plate','dimensions':{'length':60,'width':40,'thickness':8},'features':['through_hole:diameter=6,position=centered'],'constraints':[]},output_formats=('step','stl'))
    assert plan is not None
    task={'schema_version':'mcad-capability-task.v1','capability':'freecad','operation':'execute','params':{'plan':plan.model_dump(mode='json')},'inputs':{}}
    outputs={'fcstd':'application/vnd.freecad.fcstd','state':'application/json','step':'model/step','stl':'model/stl'}
    primary=McadExecutionRequest(step_key='first-native',kind='mcad_model',capability='mcad.freecad',operation='execute',source_language='json',source_code=json.dumps(task),timeout_seconds=180,outputs=tuple(McadOutputRequest(name=k,media_type=v) for k,v in outputs.items()))
    workflow,handle=await start_mcad_workflow(tenant_id=owner.tenant_id,project_id=workspace.project_id,principal_id=owner.principal_id,branch_id=workspace.branch_id,expected_base_revision_id=workspace.head_revision_id,kind='mcad.execute',idempotency_key='first-native-'+sys.argv[2],primary=primary,objective='First uncommitted native candidate acceptance',require_confirmation=False,commit_after_confirmation=False)
    assert handle is not None
    result=await asyncio.wait_for(handle.result(),timeout=180)
    assert result['status']=='succeeded',result
    print('FIRST_CANDIDATE='+json.dumps({'document_id':str(workspace.branch_id),'project_id':str(workspace.project_id),'tenant_id':str(owner.tenant_id),'base_revision_id':str(workspace.head_revision_id),'workflow_id':str(workflow),'change_set_id':result['change_set_id']}))
    await close_database()
asyncio.run(main())
'''


def main():
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    out = Path(os.environ['CAD_COEDIT_REPORT_DIR'])
    out.mkdir(parents=True, exist_ok=False)
    api = os.environ['CAD_NATIVE_E2E_API']
    assert api.startswith('cad-coedit-') and api.endswith('-api')
    session, panel_id = str(uuid4()), str(uuid4())
    title = 'First candidate acceptance ' + session[:8]
    result = subprocess.run([os.getenv('CAD_NATIVE_E2E_PODMAN','/opt/homebrew/bin/podman'),'exec','-i',api,'python','-',
        private['owner']['user']['id'],session,panel_id,title],input=SEED,text=True,capture_output=True,timeout=240)
    (out/'seed.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, 'real candidate fixture failed; inspect seed.log'
    fixture = json.loads(next(line.split('=',1)[1] for line in result.stdout.splitlines() if line.startswith('FIRST_CANDIDATE=')))
    (out/'fixture.json').write_text(json.dumps(fixture,indent=2))
    client = httpx.Client(base_url=os.environ['CAD_NATIVE_E2E_URL'],timeout=60,
        headers={'Authorization':'Bearer ' + private['owner']['token']})
    path = '/api/documents/' + fixture['document_id']
    def read(url):
        response = client.get(url)
        response.raise_for_status()
        return response.json()
    before = read(path)
    detail = read('/api/change-sets/' + fixture['change_set_id'])
    candidate = detail['candidate_revision_id']
    assert before['head_revision_id'] == fixture['base_revision_id'] and before['state_version'] == 0
    assert not before['modeling_backend'] and detail['status'] == 'pending_review'
    errors, held = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        context = browser.new_context(viewport={'width':1440,'height':1000},accept_downloads=True)
        context.tracing.start(screenshots=True,snapshots=True)
        page = context.new_page()
        page.on('pageerror',lambda error:errors.append(str(error)))
        def relay(route):
            server = route.connect_to_server()
            def outgoing(message):
                value = json.loads(message)
                if value.get('type') == 'user_message':
                    held.append(value)
                    return
                server.send(message)
            route.on_message(outgoing)
            server.on_message(route.send)
        page.route_web_socket(re.compile(r'/ws/[^/?]+$'),relay)
        try:
            page.goto(os.environ['CAD_NATIVE_E2E_WEB'])
            page.locator('input[type="text"]').fill(private['owner']['phone'])
            page.locator('input[type="password"]').fill(private['owner']['password'])
            page.locator('button[type="submit"]').click()
            page.get_by_role('button',name=title,exact=False).first.click()
            expect(page.get_by_test_id('view-identity')).to_have_attribute('data-mode','candidate',timeout=30000)
            expect(page.get_by_test_id('view-identity')).to_have_attribute('data-revision',candidate)
            expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision',candidate,timeout=90000)
            page.get_by_role('tab',name='文档',exact=True).click()
            panel = page.get_by_test_id('cloud-document-panel')
            panel.get_by_role('treeitem',name='Pad',exact=True).click()
            expect(panel.get_by_role('spinbutton',name='Pad.Length',exact=True)).to_be_disabled()
            page.locator('textarea[aria-label="询问 Agent"]:visible').first.fill('创建一个长方形板，先保持新建意图')
            expect(page.get_by_role('button',name='审查请求',exact=True)).to_be_disabled()
            page.locator('textarea[aria-label="询问 Agent"]:visible').first.fill('')
            page.screenshot(path=str(out/'uncommitted-candidate.png'))
            button = page.get_by_role('button',name='查看已提交版本',exact=True)
            button.focus();button.press('Enter')
            expect(page.get_by_test_id('view-identity')).to_have_attribute('data-mode','committed')
            expect(page.get_by_test_id('view-identity')).to_have_attribute('data-revision',before['head_revision_id'])
            page.locator('textarea[aria-label="询问 Agent"]:visible').first.fill('Create a plate 60 by 40 by 8 mm with one centered 6 mm through hole')
            page.get_by_role('button',name='审查请求',exact=True).click()
            page.get_by_role('button',name='确认并执行',exact=True).click()
            for _ in range(100):
                if held:break
                page.wait_for_timeout(100)
            assert len(held) == 1 and held[0]['operation_intent'] == 'generate',held
            assert not read(path + '/collaboration')['operations'] or all(op['id']==fixture['workflow_id'] for op in read(path+'/collaboration')['operations'])
            # Close this context to discard only the deliberately undelivered
            # request's client state. The server still holds the original candidate.
            context.tracing.stop(path=str(out/'first-candidate-trace.zip'))
            context.close()
            context = browser.new_context(viewport={'width':1440,'height':1000})
            context.tracing.start(screenshots=True,snapshots=True)
            page = context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(os.environ['CAD_NATIVE_E2E_WEB'])
            page.locator('input[type="text"]').fill(private['owner']['phone'])
            page.locator('input[type="password"]').fill(private['owner']['password'])
            page.locator('button[type="submit"]').click()
            page.get_by_role('button',name=title,exact=False).first.click()
            page.get_by_role('button',name='查看变更',exact=True).first.click()
            dialog = page.get_by_role('dialog',name='变更审查')
            dialog.get_by_role('textbox',name='审查意见',exact=True).fill('已检查真实原生首候选、参数和文件；确认首次提交。')
            dialog.get_by_role('button',name='应用修改',exact=True).click()
            expect(dialog.get_by_text('审查状态：committed',exact=True)).to_be_visible(timeout=30000)
            dialog.get_by_role('button',name='关闭',exact=True).last.click()
            after = read(path)
            assert after['head_revision_id'] == candidate and after['state_version'] == 1
            page.reload()
            expect(page.get_by_test_id('view-identity')).to_have_attribute('data-mode','committed',timeout=30000)
            expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision',candidate,timeout=90000)
            page.get_by_role('tab',name='文档',exact=True).click()
            panel=page.get_by_test_id('cloud-document-panel')
            panel.get_by_role('treeitem',name='Pad',exact=True).click()
            expect(panel.get_by_role('spinbutton',name='Pad.Length',exact=True)).to_be_enabled()
            downloaded=client.get(after['fcstd']['url']);downloaded.raise_for_status()
            assert hashlib.sha256(downloaded.content).hexdigest()==after['fcstd']['sha256']
            assert not errors,errors
            report={'status':'passed',**fixture,'candidate_revision':candidate,'before_generation':0,'after_generation':1,
                'candidate_edit_and_agent_disabled':True,'held_first_model_request_intent':held[0]['operation_intent'],
                'held_prompt_delivered_to_server':False,'candidate_committed_in_browser':True,'reload_committed_model_editable':True,
                'fcstd_sha256':after['fcstd']['sha256'],'page_errors':errors}
            (out/'report.json').write_text(json.dumps(report,indent=2))
            page.screenshot(path=str(out/'committed-first-model.png'))
            print('CAD_FIRST_CANDIDATE='+json.dumps(report),flush=True)
        except BaseException:
            page.screenshot(path=str(out/'failure.png'))
            (out/'failure.txt').write_text(page.locator('body').inner_text())
            raise
        finally:
            context.tracing.stop(path=str(out/'final-trace.zip'))
            browser.close()
            client.close()


if __name__ == '__main__':
    main()
