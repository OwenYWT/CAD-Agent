"""Five task states against real HTTP/WS, Temporal, Provider and FreeCAD.

The generation case submits a phone-case concept with explicitly missing fit
sources; it records the actual Provider outcome. The candidate case compiles a
real plate fixture and executes it in FreeCAD, then reviews/commits/edits it in
the production UI. No network responses or business results are substituted.
Credentials and traces belong in a private CAD_TASK_STATE_REPORT_DIR.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from native_first_candidate_browser import SEED

API = os.environ.get('CAD_NATIVE_E2E_URL', 'http://127.0.0.1:8040')
WEB = os.environ.get('CAD_NATIVE_E2E_WEB', 'http://127.0.0.1:8100/')
OUT = Path(os.environ['CAD_TASK_STATE_REPORT_DIR'])
PRIVATE = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())


def read(client, path):
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def login(page):
    page.goto(WEB)
    page.locator('input[type="text"]').fill(PRIVATE['owner']['phone'])
    page.locator('input[type="password"]').fill(PRIVATE['owner']['password'])
    page.locator('button[type="submit"]').click()
    expect(page.get_by_role('button', name='新建设计', exact=True)).to_be_visible(timeout=20000)


def terminal(client, page, workflow, timeout=None):
    timeout = timeout or int(os.getenv('CAD_TASK_STATE_TIMEOUT', '900'))
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        snapshot = read(client, f'/api/tasks/{workflow}/snapshot')
        if snapshot['status'] in ['succeeded', 'failed', 'timed_out', 'cancelled', 'waiting_confirmation']:
            return snapshot
        page.wait_for_timeout(500)
    raise AssertionError('Real workflow exceeded test deadline: ' + workflow)


def card(page):
    return page.locator('[data-testid="authoritative-task"]:visible')


def consistent(page, phase):
    expect(card(page)).to_have_count(1)
    expect(card(page)).to_have_attribute('data-task-phase', phase, timeout=30000)
    expect(page.get_by_test_id('viewer-task-state')).to_have_attribute('data-task-phase', phase)
    expect(page.locator('.workspace-header__agent-action:visible')).to_have_count(0)
    if phase != 'running':
        expect(card(page).locator('.animate-spin')).to_have_count(0)


def save(page, name):
    page.screenshot(path=str(OUT / (name + '.png')))
    (OUT / (name + '.txt')).write_text(page.locator('body').inner_text())


def review(page):
    page.get_by_role('button', name='审阅候选与参数变化', exact=True).click()
    dialog = page.get_by_role('dialog', name='变更审查', exact=True)
    expect(dialog.get_by_test_id('candidate-base')).to_be_visible()
    dialog.get_by_role('textbox', name='审查意见', exact=True).fill('核验真实几何、明确参数变化与证据；未做实物适配验证。')
    dialog.get_by_role('button', name='应用修改', exact=True).click()
    expect(dialog.get_by_text('审查状态：committed', exact=True)).to_be_visible(timeout=30000)
    dialog.get_by_role('button', name='关闭', exact=True).last.click()
    consistent(page, 'saved')


def generation(page, client):
    objective = '制作适配 iPhone 17 Pro Max 的手机壳，用于概念外观评估。验收任务 ' + uuid4().hex[:8]
    page.get_by_role('button', name='新建设计', exact=True).click()
    page.get_by_role('textbox', name='工程需求', exact=True).fill(objective)
    page.get_by_role('button', name='开始创建', exact=True).click()
    intake = page.get_by_role('region', name='执行前需求确认', exact=True)
    expect(intake.get_by_role('button', name='按概念外形继续', exact=True)).to_be_disabled()
    expect(intake).to_contain_text('概念外形，适配未验证')
    intake.get_by_role('textbox', name='模型用途', exact=True).fill('概念外观评估；尺寸和适配尚未核验')
    save(page, '01-waiting-for-basis')
    intake.get_by_role('checkbox', name='确认仅生成概念外形', exact=True).check()
    intake.get_by_role('button', name='按概念外形继续', exact=True).click()
    consistent(page, 'running')
    expect(card(page)).to_have_attribute('data-task-id', __import__('re').compile(r'.+'), timeout=30000)
    workflow = card(page).get_attribute('data-task-id')
    (OUT/'generation-workflow.json').write_text(json.dumps({'workflow_id': workflow, 'objective': objective}, ensure_ascii=False))
    save(page, '02-running-generation')
    result = terminal(client, page, workflow)
    (OUT/'generation-snapshot.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    basis = result['request_payload']['operation_context']['requirement_basis']
    assert basis['target'] == objective and basis['source_kind'] == 'none' and basis['concept_acknowledged'] is True
    report = {'workflow_id':workflow, 'provider_outcome':result['status'], 'basis_persisted':True}
    if result['status'] == 'failed':
        consistent(page, 'failed')
        if result['error_code'] == 'ProviderQuotaError':
            expect(card(page).get_by_role('alert')).to_have_text('模型服务额度不足，本次未生成模型')
            expect(card(page).locator('details').filter(has=page.get_by_text('错误码与调用详情',exact=True))).not_to_have_attribute('open','')
        expect(card(page).get_by_role('button', name='联系管理员', exact=True)).to_be_enabled()
        save(page, '03-failed')
        assert result['agent']['current_status'] == 'failed'
        report['error_code'] = result['error_code']
        if os.getenv('CAD_TASK_STATE_RETRY_FAILED_GENERATION') != '1':
            return report
        original = result['request_payload']
        # The production action submits immutable input, rather than “继续”.
        card(page).get_by_role('button', name='重试本次任务', exact=True).click()
        expect(card(page)).not_to_have_attribute('data-task-id', workflow, timeout=30000)
        retry = card(page).get_attribute('data-task-id')
        consistent(page, 'running')
        retried = read(client, f'/api/tasks/{retry}/snapshot')
        assert retried['request_payload'] == original
        duplicate = client.post(f'/api/tasks/{workflow}/retry',json={'idempotency_key':PRIVATE['owner']['user']['id']+':'+workflow+':retry'})
        duplicate.raise_for_status()
        assert duplicate.json()['workflow_run_id'] == retry
        report.update({'retry_id':retry,'original_payload_preserved':True,'duplicate_retry_same_workflow':True,'error_code':result['error_code']})
        save(page, 'retry-original-request')
        retried = terminal(client, page, retry)
        report['retry_outcome'] = retried['status']
        (OUT/'retry-snapshot.json').write_text(json.dumps(retried,ensure_ascii=False,indent=2))
        page.reload()
        consistent(page, 'failed' if retried['status']=='failed' else 'candidate' if retried['status']=='succeeded' else 'needs_input')
    elif result['status'] == 'succeeded':
        consistent(page,'candidate')
        save(page, 'phone-case-actual-candidate')
    else:
        consistent(page,'needs_input')
        save(page,'phone-case-server-confirmation')
    return report


def candidate(page, client):
    container = os.environ['CAD_NATIVE_E2E_API']
    assert container.startswith('cad-coedit-') and container.endswith('-api')
    session, panel_id = str(uuid4()), str(uuid4())
    title = '五状态原生验收 ' + session[:8]
    seed = subprocess.run([os.getenv('CAD_NATIVE_E2E_PODMAN','/opt/homebrew/bin/podman'),'exec','-i',container,'python','-',
        PRIVATE['owner']['user']['id'],session,panel_id,title],input=SEED,text=True,capture_output=True,timeout=240)
    (OUT/'seed.log').write_text(seed.stdout + seed.stderr)
    assert seed.returncode == 0, 'real kernel seed failed; inspect seed.log'
    fixture = json.loads(next(line.split('=',1)[1] for line in seed.stdout.splitlines() if line.startswith('FIRST_CANDIDATE=')))
    fixture.update({'session_id':session,'panel_id':panel_id,'title':title})
    (OUT/'fixture.json').write_text(json.dumps(fixture,indent=2))
    page.reload()
    page.get_by_role('button',name=title,exact=False).first.click()
    consistent(page,'candidate')
    scene = page.get_by_test_id('document-scene')
    expect(scene).to_have_attribute('data-revision', __import__('re').compile(r'.+'),timeout=90000)
    expect(page.get_by_role('tab',name='特征与属性',exact=True)).to_have_attribute('aria-selected','true')
    tree = page.get_by_test_id('cloud-document-panel')
    tree.get_by_role('treeitem',name='Pad',exact=True).click()
    expect(tree.get_by_role('spinbutton',name='Pad.Length',exact=True)).to_be_disabled()
    save(page, '04-candidate-uncommitted')
    before = read(client,'/api/documents/'+fixture['document_id'])
    assert before['state_version']==0 and not before['fcstd']
    review(page)
    saved = read(client,'/api/documents/'+fixture['document_id'])
    assert saved['state_version']==1
    page.reload()
    consistent(page,'saved')
    expect(scene).to_have_attribute('data-revision',saved['head_revision_id'],timeout=90000)
    tree = page.get_by_test_id('cloud-document-panel')
    tree.get_by_role('treeitem',name='Hole',exact=True).click()
    field = tree.get_by_role('spinbutton',name='Hole.Diameter',exact=True)
    expect(field).to_have_value('6')
    field.fill('6.5')
    expect(tree.get_by_text('我的未提交修改',exact=False)).to_be_visible()
    page.evaluate('window.__taskStateCanvas = document.querySelector("[data-testid=document-scene] canvas")')
    selected = tree.get_by_role('treeitem',name='Hole',exact=True)
    expect(selected).to_have_attribute('aria-selected','true')
    page.get_by_role('button',name='折叠检查器',exact=True).click()
    page.get_by_role('button',name='展开检查器',exact=True).click()
    expect(field).to_have_value('6.5')
    expect(selected).to_have_attribute('aria-selected','true')
    assert page.evaluate('window.__taskStateCanvas === document.querySelector("[data-testid=document-scene] canvas")')
    save(page,'05-saved-with-my-draft')
    with page.expect_response(lambda r:r.url.endswith('/operations') and r.request.method=='POST') as response:
        tree.get_by_role('button',name='提交参数变更',exact=True).click()
    assert response.value.status==202,response.value.text()
    workflow=response.value.json()['workflow_run_id']
    result=terminal(client,page,workflow)
    if result['status']=='waiting_confirmation':
        consistent(page,'needs_input')
        save(page,'manual-plan-confirmation')
        page.get_by_role('group',name='服务端执行计划确认',exact=True).get_by_role('button',name='确认并继续',exact=True).click()
        # Wait for the persisted confirmation to be consumed before polling.
        expect(card(page)).to_have_attribute('data-task-phase','running',timeout=30000)
        result=terminal(client,page,workflow)
    assert result['status']=='succeeded',result.get('error_message')
    consistent(page,'candidate')
    review(page)
    final=read(client,'/api/documents/'+fixture['document_id'])
    params=lambda doc:{p['id']:p['value'] for f in doc['features'] for p in f['parameters']}
    assert params(final)=={**params(saved),'Hole.Diameter':6.5}
    assert final['state_version']==2
    hashes={}
    for kind in ('fcstd','state','mesh'):
        data=client.get(final[kind]['url']);data.raise_for_status()
        assert hashlib.sha256(data.content).hexdigest()==final[kind]['sha256']
        (OUT/('final-'+kind+'.bin')).write_bytes(data.content)
        hashes[kind]=final[kind]['sha256']
    page.reload()
    consistent(page,'saved')
    expect(scene).to_have_attribute('data-revision',final['head_revision_id'],timeout=90000)
    save(page,'05-saved-final')
    return {'fixture':fixture,'manual_workflow_id':workflow,'original_diameter_mm':6,'final_diameter_mm':6.5,
        'unchanged_other_parameters':True,'draft_and_selection_survive_collapse':True,'canvas_not_remounted':True,
        'final_generation':final['state_version'],'artifact_sha256':hashes}


def main():
    OUT.mkdir(parents=True,exist_ok=False);OUT.chmod(0o700)
    mode=sys.argv[1] if len(sys.argv)>1 else 'candidate'
    errors=[]
    with httpx.Client(base_url=API,timeout=60,headers={'Authorization':'Bearer '+PRIVATE['owner']['token']}) as client, sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        context=browser.new_context(viewport={'width':1440,'height':1000})
        context.tracing.start(screenshots=True,snapshots=True)
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            login(page)
            report=(generation if mode=='generation' else candidate)(page,client)
            assert not errors,errors
            report.update({'status':'passed','mode':mode,'page_errors':errors})
            (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print('CAD_TASK_STATE='+json.dumps(report,ensure_ascii=False),flush=True)
        except BaseException:
            save(page,'failure')
            raise
        finally:
            context.tracing.stop(path=str(OUT/'trace.zip'));browser.close()


if __name__=='__main__':main()
