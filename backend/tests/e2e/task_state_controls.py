"""Real persisted quota failures, immutable retries, cancellation and evidence RBAC."""
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from task_state_browser import API, WEB, OUT, PRIVATE, login, read, card, consistent, save


def open_task_session(page, client, task_id):
    snapshot=read(client,f'/api/tasks/{task_id}/snapshot')
    panel_id=(snapshot['request_payload'].get('operation_context') or {}).get('panel_id')
    for session in read(client,'/api/history/sessions'):
        panels=read(client,'/api/history/sessions/'+session['id']+'/panels')
        if any(panel['id']==panel_id or panel.get('active_workflow_run_id')==task_id for panel in panels):
            page.get_by_role('button',name=session['title'],exact=False).first.click()
            return snapshot
    raise AssertionError('Persisted task has no accessible project session')


def main():
    OUT.mkdir(parents=True,exist_ok=False);OUT.chmod(0o700)
    task_id=os.environ['CAD_QUOTA_WORKFLOW']
    evidence_task=os.environ['CAD_EVIDENCE_WORKFLOW']
    errors=[]
    with httpx.Client(base_url=API,timeout=60,headers={'Authorization':'Bearer '+PRIVATE['owner']['token']}) as client, sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        context=browser.new_context(viewport={'width':1440,'height':1000})
        context.tracing.start(screenshots=True,snapshots=True)
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        try:
            login(page)
            original=open_task_session(page,client,task_id)
            assert original['status']=='failed' and original['error_code']=='ProviderQuotaError'
            retry=os.getenv('CAD_CONTROLS_RESUME_RETRY')
            if not retry:
                consistent(page,'failed')
                expect(card(page).get_by_role('alert')).to_have_text('模型服务额度不足，本次未生成模型')
                expect(card(page).get_by_text('ProviderQuotaError',exact=True)).to_be_hidden()
                expect(page.locator('.workspace-header__export-action')).to_be_disabled()
                card(page).get_by_role('button',name='联系管理员',exact=True).click()
                expect(card(page).get_by_role('button',name='复制故障摘要',exact=True)).to_be_enabled()
                save(page,'03-real-quota-failure')
                with page.expect_response(lambda r:r.url.endswith('/retry') and r.request.method=='POST') as receipt:
                    card(page).get_by_role('button',name='重试本次任务',exact=True).click()
                assert receipt.value.status==202,receipt.value.text()
                retry=receipt.value.json()['workflow_run_id']
                (OUT/'retry-receipt.json').write_text(json.dumps(receipt.value.json(),indent=2))
            retried=read(client,f'/api/tasks/{retry}/snapshot')
            assert retried['request_payload']==original['request_payload']
            if not os.getenv('CAD_CONTROLS_RESUME_RETRY'):
                consistent(page,'running')
                expect(card(page).get_by_role('alert')).to_have_count(0)
                save(page,'retry-without-old-error')
                card(page).get_by_role('button',name='取消任务',exact=True).click()
            deadline=time.monotonic()+180
            while time.monotonic()<deadline and read(client,f'/api/tasks/{retry}/snapshot')['status']!='cancelled':
                page.wait_for_timeout(500)
            consistent(page,'failed')
            expect(card(page).get_by_role('alert')).to_have_text('任务已取消')
            key=PRIVATE['owner']['user']['id']+':'+task_id+':retry'
            duplicate=client.post(f'/api/tasks/{task_id}/retry',json={'idempotency_key':key});duplicate.raise_for_status()
            assert duplicate.json()['workflow_run_id']==retry and duplicate.json()['status']=='cancelled'
            page.reload();consistent(page,'failed')
            assert card(page).get_attribute('data-task-id')==retry
            assert read(client,f'/api/tasks/{task_id}/snapshot')['error_code']=='ProviderQuotaError'
            save(page,'cancelled-retry-restored')
            checks=read(client,f'/api/tasks/{evidence_task}/snapshot')['agent']['validations']
            reports=[]
            for check in checks:
                evidence=read(client,f"/api/tasks/{evidence_task}/validations/{check['evidence_id']}")
                assert evidence['selected_for_revision'] and evidence['revision_id']
                assert evidence['evidence_hash']==check['evidence_hash']
                assert evidence['report'] and 'runtime_provenance' not in evidence['report']
                reports.append(evidence)
            missing=client.get(f'/api/tasks/{evidence_task}/validations/{uuid4()}')
            assert missing.status_code==404
            with httpx.Client(base_url=API,timeout=60,headers={'Authorization':'Bearer '+PRIVATE['guest']['token'], 'X-Workspace-Tenant':PRIVATE['tenant_id']}) as other:
                denied=other.get(f"/api/tasks/{evidence_task}/validations/{checks[0]['evidence_id']}")
                assert denied.status_code in [403,404]
                denied_retry=other.post(f'/api/tasks/{task_id}/retry',json={'idempotency_key':str(uuid4())})
                assert denied_retry.status_code in [403,404]
            page.reload();open_task_session(page,client,evidence_task)
            card(page).get_by_text(f'检查结果（{len(checks)}）',exact=True).click()
            card(page).get_by_role('button',name='查看检查对象、版本与规则',exact=True).first.click()
            expect(card(page).get_by_test_id('validation-evidence')).to_contain_text('检查已绑定版本')
            save(page,'bound-validation-evidence')
            assert not errors,errors
            report={'status':'passed','resumed_existing_retry':bool(os.getenv('CAD_CONTROLS_RESUME_RETRY')),'original_quota_workflow':task_id,'retry_workflow':retry,'original_payload_preserved':True,
                'duplicate_retry_same_task_and_actual_status':True,'cancel_confirmed_after_reload':True,
                'old_quota_not_current_error':True,'evidence_workflow':evidence_task,
                'bound_checks':reports,'unauthorized_evidence':denied.status_code,'unauthorized_retry':denied_retry.status_code,'page_errors':errors}
            (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print('CAD_TASK_CONTROLS='+json.dumps({k:v for k,v in report.items() if k!='bound_checks'},ensure_ascii=False))
        except BaseException:
            save(page,'failure');raise
        finally:
            context.tracing.stop(path=str(OUT/'trace.zip'));browser.close()


if __name__=='__main__':main()
