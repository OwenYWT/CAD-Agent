from pathlib import Path
import os,json,subprocess,sys
from uuid import uuid4
import httpx
from playwright.sync_api import sync_playwright,expect
r=Path(__file__).parent
private=json.loads(Path('/private/tmp/cad-fixes-20260923/complete-capabilities-20260927/work/browser-private.json').read_text())
title='Engineering evidence '+str(uuid4())[:8]
source='''import asyncio,json,sys,importlib.util
from uuid import uuid4
from types import SimpleNamespace
from sqlalchemy import text
from app.config import settings
from app.domain.identity import user_principal
from app.services.durable_submission import ensure_workspace_identity
from app.db import tenant_transaction,close_database
spec=importlib.util.spec_from_file_location('controlled_contract','/app/backend/tests/integration/test_temporal_mcadd_workflow.py')
t=importlib.util.module_from_spec(spec);spec.loader.exec_module(t)
async def main():
    owner=user_principal(sys.argv[1])
    workspace=await ensure_workspace_identity(owner,session_id=str(uuid4()),panel_id=str(uuid4()),title=sys.argv[2],user_id=sys.argv[1])
    async def seed(label):
        return owner,workspace.project_id,SimpleNamespace(branch_id=workspace.branch_id,revision_id=workspace.head_revision_id)
    t._seed_project=seed
    settings.temporal_agent_v2_task_queue='measurement-browser-'+str(uuid4())
    for name in ('moonshot_api_key','dashscope_api_key','azure_openai_api_key'):setattr(settings,name,None)
    await t.test_native_engineering_acceptance_gates_commit(6,'typed',output_formats=('stl',))
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        row=(await conn.execute(text("SELECT v.id,v.workflow_run_id FROM agent_validation_evidence v JOIN workflow_runs w ON w.id=v.workflow_run_id WHERE w.project_id=:p AND v.gate='geometry'"),{'p':workspace.project_id})).mappings().one()
        data={k:str(v) for k,v in row.items()}
    print('MEASUREMENT_BROWSER='+json.dumps({**data,'document_id':str(workspace.branch_id)}))
    await close_database()
asyncio.run(main())
'''
res=subprocess.run(['/private/tmp/cad-fixes-20260923/bin/docker','exec','-i','cad-tools-browser-api','python','-',private['owner']['user']['id'],title],input=source,text=True,capture_output=True)
(r/'measurement-browser-seed.log').write_text(res.stdout+res.stderr)
assert res.returncode==0,'seed failed; see private work log'
fixture=json.loads(next(line.split('=',1)[1] for line in res.stdout.splitlines() if line.startswith('MEASUREMENT_BROWSER=')))
with httpx.Client(base_url='http://127.0.0.1:18860',headers={'Authorization':'Bearer '+private['owner']['token']}) as api:
    response=api.get(f"/api/tasks/{fixture['workflow_run_id']}/validations/{fixture['id']}");response.raise_for_status();evidence=response.json()
    assert evidence['selected_for_revision']
    assert evidence['report']['acceptance_contract'] and evidence['report']['request_sha256']
    assert all(item['outcome']=='passed' for item in evidence['report']['acceptance']['evidence'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
    page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto('http://127.0.0.1:18870')
        page.locator('input[type=text]').fill(private['owner']['phone']);page.locator('input[type=password]').fill(private['owner']['password'])
        page.locator('button[type=submit]').click();page.get_by_role('button',name=title,exact=False).first.click()
        card=page.locator(f'[data-task-id="{fixture["workflow_run_id"]}"]')
        expect(card).to_be_visible(timeout=30000)
        card.locator('summary').filter(has_text='检查结果').click()
        card.get_by_role('button',name='查看检查对象、版本与规则').first.click()
        rows=card.get_by_test_id('engineering-measurement')
        expect(rows).to_have_count(len(evidence['report']['acceptance']['evidence']),timeout=15000)
        assert '目标：6 mm' in rows.all_text_contents()[2] or any('目标：6 mm' in text for text in rows.all_text_contents())
        assert any('实测：6 mm' in text for text in rows.all_text_contents())
        assert not errors,errors
        page.screenshot(path=str(r/'measurement-browser.png'))
        (r/'measurement-browser.json').write_text(json.dumps({'passed':True,'fixture':fixture,'evidence':evidence,'visible_measurements':rows.all_text_contents(),'page_errors':errors},ensure_ascii=False,indent=2))
    except BaseException:
        (r/'measurement-browser-failure.txt').write_text(page.locator('body').inner_text());raise
    finally:browser.close()
print('MEASUREMENT_BROWSER_PASSED')
