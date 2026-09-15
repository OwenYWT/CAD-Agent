"""Real legacy-input failure -> history recovery -> reviewed HTTP/WS submission.

The isolated fixture intentionally invokes the lower-level Temporal entry that
old clients used, to reproduce a real Provider rejection of bare 'continue'.
No Provider result, browser response or workflow outcome is substituted.
Never point the fixture at production. Credentials and traces stay private.
"""
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright

SEED = '''import asyncio,json,sys
from app.domain.identity import user_principal
from app.principal_context import bind_principal
from app.services.durable_submission import ensure_workspace_identity
from app.storage.postgres_history import save_message
from app.workflows.temporal import start_mcad_agent_v2_workflow,OperationContextV1
async def main():
    p=user_principal(sys.argv[1]);bind_principal(p)
    w=await ensure_workspace_identity(p,session_id=sys.argv[2],panel_id=sys.argv[3],title=sys.argv[4],user_id=sys.argv[1])
    await save_message(sys.argv[3],'user',sys.argv[5])
    context=OperationContextV1(rule='explicit_ui_intent',source_channel='session_websocket',panel_id=sys.argv[3],requested_operation='generate',resolved_operation='generate',submission_modeling_backend='auto',base_revision_id=w.head_revision_id,base_source_kind='none')
    task,handle=await start_mcad_agent_v2_workflow(tenant_id=p.tenant_id,principal_id=p.principal_id,project_id=w.project_id,branch_id=w.branch_id,expected_base_revision_id=w.head_revision_id,kind='mcad.agent.v2.generate',idempotency_key='legacy-'+sys.argv[2],operation='generate',objective='当前上下文：机械设计。请先限定修改范围，再执行以下请求并验证结果：继续',modeling_backend='auto',operation_context=context)
    print('FIXTURE='+json.dumps({'task':str(task),'document':str(w.branch_id),'session':sys.argv[2],'title':sys.argv[4]}),flush=True)
asyncio.run(main())
'''


def main():
    private=json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    container=os.environ.get('CAD_NATIVE_E2E_API','cad-coedit-20260913-api')
    assert container.startswith('cad-coedit-') and container.endswith('-api')
    api=os.environ.get('CAD_NATIVE_E2E_URL','http://127.0.0.1:8040')
    web=os.environ.get('CAD_NATIVE_E2E_WEB','http://127.0.0.1:8100')
    assert api.startswith('http://127.0.0.1:') and web.startswith('http://127.0.0.1:')
    out=Path(os.environ['CAD_CONTINUATION_REPORT_DIR']);out.mkdir(mode=0o700,parents=True,exist_ok=False)
    objective='创建一个长 60 mm、宽 40 mm、厚 8 mm 的矩形板，中心有一个直径 6 mm 的贯穿孔。保持这些尺寸，导出 STEP 和 STL。'
    if os.environ.get('CAD_CONTINUATION_FIXTURE'):
        fixture=json.loads(Path(os.environ['CAD_CONTINUATION_FIXTURE']).read_text())
    else:
        session,panel=str(uuid4()),str(uuid4());title='Continuation recovery '+session[:8]
        seed=subprocess.run(['podman','exec','-i',container,'python','-',private['owner']['user']['id'],session,panel,title,objective],input=SEED,text=True,capture_output=True,timeout=390)
        (out/'seed.log').write_text(seed.stdout+seed.stderr)
        fixture=json.loads(next(line.split('=',1)[1] for line in seed.stdout.splitlines() if line.startswith('FIXTURE=')))
    (out/'fixture.json').write_text(json.dumps(fixture))
    errors=[]
    with httpx.Client(base_url=api,timeout=60) as client,sync_playwright() as pw:
        login=client.post('/api/auth/login/password',json={'phone':private['owner']['phone'],'password':private['owner']['password']});login.raise_for_status()
        client.headers['Authorization']='Bearer '+login.json()['token']
        def read(path):
            r=client.get(path);r.raise_for_status();return r.json()
        original=read('/api/tasks/'+fixture['task']+'/snapshot')
        deadline=time.monotonic()+360
        confirmed=False
        while original['status'] in ('pending','planning','running','waiting_confirmation') and time.monotonic()<deadline:
            if original['status']=='waiting_confirmation' and not confirmed:
                response=client.post('/api/tasks/'+fixture['task']+'/confirmation',json={'accepted':True,'note':'Isolated legacy-input rejection reproduction'});response.raise_for_status();confirmed=True
            time.sleep(1);original=read('/api/tasks/'+fixture['task']+'/snapshot')
        assert original['status']=='failed',original['status']
        assert 'valid plan' in original['error_message'],original['error_message']
        denied=client.post('/api/tasks/'+fixture['task']+'/retry',json={'idempotency_key':str(uuid4())})
        assert denied.status_code==422 and '缺少建模目标' in denied.text,(denied.status_code,denied.text)
        browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        context=browser.new_context(viewport={'width':1440,'height':1000});context.tracing.start(screenshots=True,snapshots=True)
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        try:
            page.goto(web);page.locator('input[type="text"]').fill(private['owner']['phone']);page.locator('input[type="password"]').fill(private['owner']['password']);page.locator('button[type="submit"]').click()
            page.get_by_role('button',name=fixture['title'],exact=False).first.click()
            card=page.locator('[data-testid="authoritative-task"]:visible')
            expect(card).to_have_attribute('data-task-id',fixture['task'],timeout=30000)
            expect(card.get_by_role('button',name='重试本次任务',exact=True)).to_have_count(0)
            expect(card).to_contain_text('缺少具体建模目标')
            page.screenshot(path=str(out/'legacy-failure.png'))
            card.get_by_role('button',name='恢复历史需求并确认',exact=True).click()
            intake=page.get_by_role('region',name='执行前需求确认',exact=True)
            expect(intake.get_by_role('textbox',name='建模目标',exact=True)).to_have_value(objective)
            expect(intake.get_by_role('button',name='按概念外形继续',exact=True)).to_be_disabled()
            before=read('/api/documents/'+fixture['document'])
            assert before.get('active_workflow_run_id') in (None,fixture['task'])
            page.screenshot(path=str(out/'restored-requirement.png'))
            intake.get_by_role('checkbox',name='确认仅生成概念外形',exact=True).check()
            intake.get_by_role('button',name='按概念外形继续',exact=True).click()
            expect(card).to_have_attribute('data-task-phase','running',timeout=30000)
            expect(card).not_to_have_attribute('data-task-id',fixture['task'],timeout=30000)
            deadline=time.monotonic()+30
            task=card.get_attribute('data-task-id')
            while not task and time.monotonic()<deadline:
                page.wait_for_timeout(200);task=card.get_attribute('data-task-id')
            assert task
            restored=read('/api/tasks/'+task+'/snapshot')
            assert restored['request_payload']['objective']==objective
            assert restored['request_payload']['operation_context']['requirement_basis']['target']==objective
            assert read('/api/tasks/'+fixture['task']+'/snapshot')['request_payload']==original['request_payload']
            deadline=time.monotonic()+600
            while restored['status'] in ('pending','planning','running') and time.monotonic()<deadline:
                page.wait_for_timeout(1000);restored=read('/api/tasks/'+task+'/snapshot')
            (out/'restored-task.json').write_text(json.dumps(restored,ensure_ascii=False,indent=2))
            assert restored['status']=='succeeded',(restored['status'],restored.get('error_message'))
            expect(card).to_have_attribute('data-task-phase','candidate',timeout=30000)
            page.screenshot(path=str(out/'recovered-candidate.png'))
            assert not errors,errors
            report={'status':'passed','legacy_task':fixture['task'],'restored_task':task,'legacy_retry_http':denied.status_code,'history_confirmed_before_submission':True,'objective_and_basis_preserved':True,'old_task_unchanged':True,'new_task_status':restored['status'],'page_errors':errors}
            (out/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
        finally:
            context.tracing.stop(path=str(out/'trace.zip'));browser.close()


if __name__=='__main__':main()
