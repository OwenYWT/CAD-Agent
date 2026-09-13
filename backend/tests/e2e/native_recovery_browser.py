"""Real history restore, lost WS receipt, partial apply, rollback and ABA guard.

Faults only drop a real server acknowledgement and abort one commit transport.
Every surviving message/request goes to the actual API, Temporal and FreeCAD.
No provider, CAD response, candidate or database result is substituted.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import time
from uuid import UUID, uuid4

import asyncpg
import httpx
from playwright.sync_api import expect, sync_playwright


def main():
    base = os.environ['CAD_NATIVE_E2E_URL']
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    env = json.loads(Path(os.environ['CAD_NATIVE_E2E_HOST_ENV']).read_text())
    assert '/cad_coedit_live_' in env['DATABASE_URL'] and base.startswith('http://127.0.0.1:')
    report = Path(os.environ['CAD_COEDIT_REPORT_DIR']); report.mkdir(parents=True,exist_ok=True)
    evidence = {}
    def record(name, facts):
        evidence[name]=facts;(report/'report.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
        print(name,json.dumps(facts,ensure_ascii=False),flush=True)
    client=httpx.Client(base_url=base,headers={'Authorization':'Bearer '+private['owner']['token']},timeout=60)
    path='/api/documents/'+private['document_id']
    def read(url):
        response=client.get(url);response.raise_for_status();return response.json()
    def post(url, body=None):
        response=client.post(url,json=body);response.raise_for_status();return response.json()
    def parameters(document): return {p['id']:p['value'] for f in document['features'] for p in f['parameters']}
    def wait(task_id, page=None):
        deadline=time.monotonic()+600;last=None
        while time.monotonic()<deadline:
            task=read('/api/tasks/'+task_id+'/snapshot')
            if task['status']!=last: last=task['status'];print('task',task_id,last,flush=True)
            if last=='waiting_confirmation':
                if page:
                    button=page.get_by_role('button',name='确认并继续',exact=True)
                    expect(button).to_be_visible(timeout=20000);button.click()
                else: post('/api/tasks/'+task_id+'/confirmation',{'accepted':True,'note':'Review the real exact-edit validation plan'})
            if last in {'succeeded','failed','cancelled','timed_out'}:
                assert last=='succeeded',task
                return task
            if page: page.wait_for_timeout(1000)
            else: time.sleep(1)
        raise AssertionError(task)
    before=read(path)
    source=None
    for snapshot in reversed(read('/api/history/panels/'+private['panel_id']+'/snapshots')):
        if snapshot['id']==before['head_revision_id']:continue
        view=read(path+'/revisions/'+snapshot['id'])
        if view['snapshot'].get('files',{}).get('fcstd') and parameters(view).get('Hole.Diameter')==9 and parameters(view)!=parameters(before):
            source=snapshot;source_view=view;break
    assert source, 'Need a real 9 mm hole historical revision with different native dimensions'
    # Keep a real solved candidate based on R0. After R0 -> restored R1 -> R0,
    # it must fail final acceptance even though the head UUID is R0 again.
    stale_submission={'workflow_run_id':os.environ['CAD_NATIVE_E2E_ABA_WORKFLOW']} if os.environ.get('CAD_NATIVE_E2E_ABA_WORKFLOW') else post(path+'/operations',{
        'action':'parameters.update','expected_base_revision_id':before['head_revision_id'],
        'expected_state_version':before['state_version'],'idempotency_key':str(uuid4()),
        'modification':{'expected_state_sha256':before['parameter_state_sha256'],
            'parameter_updates':[{'parameter_id':'Pad.Length','value':parameters(before)['Pad.Length']+1}]}})
    stale_task=wait(stale_submission['workflow_run_id'])
    stale_detail=read('/api/change-sets/'+stale_task['change_set']['id'])
    assert stale_detail['base_revision_id']==before['head_revision_id'] and stale_detail['base_state_version']==before['state_version']
    record('aba_fixture',{'workflow_id':stale_task['id'],'change_set_id':stale_task['change_set']['id'],
        'original_revision':before['head_revision_id'],'original_generation':before['state_version']})

    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_page(viewport={'width':1440,'height':960},accept_downloads=True)
        page.set_default_timeout(60000)
        page.context.tracing.start(screenshots=True,snapshots=True)
        errors=[];requests=[];receipts=[];fault={'receipt':None,'commit_aborted':False}
        page.on('pageerror',lambda e:errors.append(str(e)))
        def socket(route):
            server=route.connect_to_server()
            def request(message):
                item=json.loads(message)
                if item.get('type') in {'restore_revision','recover_submission'}: requests.append(item)
                server.send(message)
            def response(message):
                item=json.loads(message)
                if item.get('type')=='task_submitted':
                    if fault['receipt'] is None:
                        fault['receipt']=item['data']
                        route.close(code=1011,reason='Acceptance test: dropped real receipt')
                        server.close();return
                    receipts.append(item['data'])
                route.send(message)
            route.on_message(request);server.on_message(response)
        page.route_web_socket(re.compile(r'/ws/[^/?]+$'),socket)
        try:
            page.goto(base)
            page.locator('input[type="text"]').fill(private['owner']['phone'])
            page.locator('input[type="password"]').fill(private['owner']['password'])
            page.locator('input[type="password"]').press('Enter')
            page.get_by_role('button',name='Create a rectangular plate',exact=False).first.click()
            expect(page.get_by_test_id('view-identity')).to_have_attribute('data-revision',before['head_revision_id'],timeout=20000)
            page.get_by_role('tab',name='版本',exact=True).click()
            row=page.locator('div.rounded-lg.border.p-3').filter(has_text='修订 #'+str(source['version'])+' ·').last
            row.get_by_role('button',name='查看此版本',exact=True).click()
            expect(page.get_by_test_id('view-identity')).to_have_attribute('data-mode','history')
            expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision',source['id'],timeout=60000)
            composer=page.locator('textarea[aria-label="询问 Agent"]:visible').first
            composer.fill('将这个历史模型的厚度改为 20 mm')
            expect(page.get_by_role('button',name='审查请求',exact=True)).to_be_disabled()
            composer.fill('')
            row.get_by_role('button',name='从此版本生成恢复候选',exact=True).click()
            deadline=time.monotonic()+60
            while fault['receipt'] is None and time.monotonic()<deadline:page.wait_for_timeout(100)
            assert fault['receipt'], 'Real restore task not submitted'
            original=requests[0]
            assert original['type']=='restore_revision' and original['source_revision_id']==source['id']
            assert original['expected_base_revision_id']==before['head_revision_id'] and original['expected_state_version']==before['state_version']
            task_id=fault['receipt']['workflow_run_id']
            # Reload before receiving the acknowledgement. sessionStorage must
            # retain the original request and recover its exact server receipt.
            page.reload()
            page.get_by_role('button',name='Create a rectangular plate',exact=False).first.click()
            deadline=time.monotonic()+60
            while not receipts and time.monotonic()<deadline:page.wait_for_timeout(100)
            assert receipts and receipts[0]['workflow_run_id']==task_id and receipts[0]['submission_id']==original['idempotency_key'],receipts
            assert any(r['type']=='recover_submission' and r['idempotency_key']==original['idempotency_key'] for r in requests),requests
            assert len([r for r in requests if r['type']=='restore_revision'])==1,requests
            task=wait(task_id,page)
            candidate=task['change_set']['candidate_revision_id'];change_id=task['change_set']['id']
            assert parameters(read(path+'/revisions/'+candidate))==parameters(source_view)
            assert read(path)['head_revision_id']==before['head_revision_id']
            record('lost_receipt',{'workflow_id':task_id,'submission_id':original['idempotency_key'],
                'restore_transmissions':1,'receipt_recovered_after_refresh':True,'source_revision':source['id'],'candidate_revision':candidate})

            def fail_first_commit(route):
                if not fault['commit_aborted']:
                    fault['commit_aborted']=True;route.abort('connectionreset')
                else:route.continue_()
            page.route('**/api/change-sets/'+change_id+'/commit',fail_first_commit)
            page.get_by_test_id('cloud-document-panel').get_by_role('button',name='查看变更',exact=True).first.click()
            dialog=page.get_by_role('dialog',name='变更审查')
            expect(dialog).to_be_visible()
            dialog.get_by_role('textbox',name='审查意见',exact=True).fill('核对实际 FCStd 恢复来源、候选几何及门禁，故障恢复后继续提交同一候选。')
            dialog.get_by_role('button',name='应用修改',exact=True).click()
            expect(dialog.get_by_text('已接受，尚未提交。重试将继续提交同一候选。',exact=True)).to_be_visible(timeout=30000)
            expect(dialog.get_by_role('button',name='继续提交此候选',exact=True)).to_be_enabled(timeout=15000)
            assert fault['commit_aborted'] and read('/api/change-sets/'+change_id)['status']=='accepted'
            assert read(path)['head_revision_id']==before['head_revision_id']
            page.screenshot(path=str(report/'01-accepted-commit-interrupted.png'))
            dialog.get_by_role('button',name='继续提交此候选',exact=True).click()
            expect(dialog.get_by_text('审查状态：committed',exact=True)).to_be_visible(timeout=30000)
            dialog.get_by_role('button',name='关闭',exact=True).last.click()
            expect(page.get_by_test_id('view-identity')).to_have_attribute('data-revision',candidate,timeout=20000)
            restored=read(path)
            assert restored['state_version']==before['state_version']+1 and parameters(restored)==parameters(source_view)
            record('partial_apply',{'accepted_persisted':True,'retry_committed_same_candidate':True,'head_advanced_once':True,
                'state_version':restored['state_version'],'revision_id':candidate})

            page.get_by_role('button',name='导出',exact=True).first.click()
            export=page.get_by_role('dialog',name='导出工程产物')
            expect(export).to_be_visible()
            expect(export).to_contain_text(candidate[:8])
            download_row=export.locator('div.flex.min-h-\\[66px\\]').filter(has_text='model.step')
            with page.expect_download() as download:
                download_row.get_by_role('button',name='下载',exact=True).click()
            saved=report/'restored.step';download.value.save_as(saved)
            immutable=read(path+'/revisions/'+candidate)
            actual=client.get(immutable['snapshot']['files']['step']);actual.raise_for_status()
            assert saved.read_bytes()==actual.content
            export.get_by_role('button',name='关闭',exact=True).last.click()
            record('browser_export',{'revision_id':candidate,'sha256':hashlib.sha256(actual.content).hexdigest(),'size_bytes':len(actual.content)})

            page.get_by_test_id('cloud-document-panel').get_by_role('button',name='查看变更',exact=True).first.click()
            dialog=page.get_by_role('dialog',name='变更审查')
            dialog.get_by_role('textbox',name='审查意见',exact=True).fill('实际回退本次恢复提交，并检查旧候选的 ABA 守卫。')
            dialog.get_by_role('button',name='回滚',exact=True).click()
            expect(dialog.get_by_text('审查状态：rolled_back',exact=True)).to_be_visible(timeout=30000)
            dialog.get_by_role('button',name='关闭',exact=True).last.click()
            expect(page.get_by_test_id('view-identity')).to_have_attribute('data-revision',before['head_revision_id'],timeout=20000)
            reverted=read(path)
            assert reverted['state_version']==before['state_version']+2 and parameters(reverted)==parameters(before)
            stale_change=stale_task['change_set']['id']
            rejected=client.post('/api/change-sets/'+stale_change+'/accept',json={'note':'This stale candidate must be blocked after ABA'})
            assert rejected.status_code==409,rejected.text
            stale=read('/api/change-sets/'+stale_change)
            assert stale['status']=='pending_review' and not stale['base_is_current']
            assert read(path)['state_version']==reverted['state_version']
            record('rollback_aba',{'restored_original_revision':before['head_revision_id'],'new_generation':reverted['state_version'],
                'old_candidate_accept_status':rejected.status_code,'original_generation':before['state_version']})
            page.screenshot(path=str(report/'02-rollback-original-head.png'))
            assert not errors,errors
            record('browser_errors',errors)

            async def durable_count():
                connection=await asyncpg.connect(env['DATABASE_URL'].replace('postgresql+asyncpg:','postgresql:'))
                try:
                    count=await connection.fetchval('SELECT count(*) FROM workflow_runs WHERE idempotency_key=$1',original['idempotency_key'])
                    assert count==1
                    attempts=await connection.fetch('''SELECT s.step_key,s.kind,a.attempt_number,a.status
                        FROM execution_attempts a JOIN step_runs s ON s.id=a.step_run_id
                        WHERE a.workflow_run_id=$1 ORDER BY s.step_index''',UUID(task_id))
                    modeling=[a for a in attempts if a['kind']=='agent_freecad_operations']
                    assert len(modeling)==1 and len({a['step_key'] for a in attempts})==len(attempts)
                    assert all(a['attempt_number']==1 and a['status']=='succeeded' for a in attempts),attempts
                    return {'workflows_for_original_key':count,'native_restore_attempts':len(modeling),
                            'separate_validation_attempts':len(attempts)-len(modeling),'steps':[dict(a) for a in attempts]}
                finally:await connection.close()
            with ThreadPoolExecutor(max_workers=1) as pool:
                record('durable_deduplication',pool.submit(lambda:asyncio.run(durable_count())).result())
        except Exception:
            page.screenshot(path=str(report/'failure.png'));raise
        finally:
            page.context.tracing.stop(path=str(report/'browser-trace.zip'))
            browser.close();client.close()


if __name__=='__main__':main()
