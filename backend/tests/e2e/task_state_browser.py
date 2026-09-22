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
    back = page.get_by_role('button',name='返回 Agent',exact=True)
    if back.is_visible(): back.click()
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
    back=page.get_by_role('button',name='返回 Agent',exact=True)
    if back.is_visible(): back.click()
    page.get_by_role('button', name='审阅候选与参数变化', exact=True).click()
    dialog = page.get_by_role('dialog', name='变更审查', exact=True)
    expect(dialog.get_by_test_id('candidate-base')).to_be_visible()
    dialog.get_by_role('textbox', name='审查意见', exact=True).fill('核验真实几何、明确参数变化与证据；未做实物适配验证。')
    expect(dialog.get_by_role('button', name='接受变更', exact=True)).to_have_count(0)
    expect(dialog.get_by_role('button', name='提交版本', exact=True)).to_have_count(0)
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


def geometry_intake(page, client):
    import io
    import trimesh
    objective='创建一个 100×60×3 mm 的长方形板，不添加孔、槽或其他特征。'
    page.get_by_role('button',name='新建设计',exact=True).click()
    page.get_by_role('textbox',name='工程需求',exact=True).fill(objective)
    page.get_by_role('button',name='开始创建',exact=True).click()
    intake=page.get_by_role('region',name='执行前需求确认',exact=True)
    expect(intake.get_by_role('textbox',name='关键尺寸依据',exact=True)).to_have_value('100×60×3 mm')
    assert '适配未验证' not in intake.inner_text()
    intake.get_by_role('textbox',name='关键尺寸依据',exact=True).fill('100×60×4 mm')
    expect(intake.get_by_role('textbox',name='建模目标',exact=True)).to_have_value(objective.replace('100×60×3','100×60×4'))
    intake.get_by_role('button',name='确认并执行',exact=True).click()
    consistent(page,'running')
    expect(card(page)).to_have_attribute('data-task-id',__import__('re').compile('.+'),timeout=30000)
    workflow=card(page).get_attribute('data-task-id')
    (OUT/'workflow.json').write_text(json.dumps({'workflow_id':workflow}))
    snapshot=terminal(client,page,workflow)
    if snapshot['status']=='waiting_confirmation':
        page.get_by_role('group',name='服务端执行计划确认',exact=True).get_by_role('button',name='确认并继续',exact=True).click()
        expect(card(page)).to_have_attribute('data-task-phase','running',timeout=30000)
        snapshot=terminal(client,page,workflow)
    (OUT/'snapshot.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
    basis=snapshot['request_payload']['operation_context']['requirement_basis']
    assert basis['design_scope']=='geometry' and basis['source_kind']=='user_specification'
    assert basis['dimensions']=='100×60×4 mm' and not basis['concept_acknowledged']
    assert snapshot['status']=='succeeded',snapshot.get('error_message')
    consistent(page,'candidate');review(page)
    document=read(client,'/api/documents/'+snapshot['request_payload']['branch_id'])
    response=client.get(document['mesh']['url']);response.raise_for_status()
    assert hashlib.sha256(response.content).hexdigest()==document['mesh']['sha256']
    mesh=trimesh.load(io.BytesIO(response.content),file_type='stl',force='mesh')
    assert all(abs(a-b)<.01 for a,b in zip(sorted(mesh.extents),[4,60,100])),mesh.extents
    save(page,'explicit-geometry-saved')
    return {'workflow_id':workflow,'basis_persisted':True,'extent_mm':mesh.extents.tolist(),'state_version':document['state_version'],'mesh_sha256':document['mesh']['sha256']}


def candidate(page, client):
    faults = os.environ.get('CAD_BROWSER_FAULT_CONTRACTS') == '1'
    session, panel_id = str(uuid4()), str(uuid4())
    title = '五状态原生验收 ' + session[:8]
    from runtime_fixture import run_native_seed
    seed = run_native_seed(SEED, [PRIVATE['owner']['user']['id'],session,panel_id,title])
    (OUT/'seed.log').write_text(seed.stdout + seed.stderr)
    assert seed.returncode == 0, 'real kernel seed failed; inspect seed.log'
    fixture = json.loads(next(line.split('=',1)[1] for line in seed.stdout.splitlines() if line.startswith('FIRST_CANDIDATE=')))
    fixture.update({'session_id':session,'panel_id':panel_id,'title':title})
    other = None
    if faults:
        other_title = '隔离面板 ' + uuid4().hex[:8]
        seeded = run_native_seed(SEED,[PRIVATE['owner']['user']['id'],str(uuid4()),str(uuid4()),other_title])
        assert seeded.returncode == 0, seeded.stderr
        other = json.loads(next(line.split('=',1)[1] for line in seeded.stdout.splitlines() if line.startswith('FIRST_CANDIDATE=')))
    (OUT/'fixture.json').write_text(json.dumps(fixture,indent=2))
    page.reload()
    page.get_by_role('button',name=title,exact=False).first.click()
    consistent(page,'candidate')
    scene = page.get_by_test_id('document-scene')
    expect(scene).to_have_attribute('data-revision', __import__('re').compile(r'.+'),timeout=90000)
    page.get_by_role('button',name='属性',exact=True).click()
    tree = page.locator('[data-testid=cloud-document-panel]:visible')
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
    page.get_by_role('button',name='属性',exact=True).click()
    tree = page.locator('[data-testid=cloud-document-panel]:visible')
    tree.get_by_role('treeitem',name='Hole',exact=True).click()
    field = tree.get_by_role('spinbutton',name='Hole.Diameter',exact=True)
    expect(field).to_have_value('6')
    field.fill('6.5')
    expect(tree.get_by_text('我的未提交修改',exact=False)).to_be_visible()
    page.evaluate('window.__taskStateCanvas = document.querySelector("[data-testid=document-scene] canvas")')
    selected = tree.get_by_role('treeitem',name='Hole',exact=True)
    expect(selected).to_have_attribute('aria-selected','true')
    page.get_by_role('button',name='返回 Agent',exact=True).click()
    page.get_by_role('button',name='属性',exact=True).click()
    expect(field).to_have_value('6.5')
    expect(selected).to_have_attribute('aria-selected','true')
    assert page.evaluate('window.__taskStateCanvas === document.querySelector("[data-testid=document-scene] canvas")')
    save(page,'05-saved-with-my-draft')
    if faults:
        page.get_by_role('button',name=other_title,exact=False).first.click()
        leave = page.get_by_role('dialog',name='保留编辑草稿',exact=True)
        leave.get_by_role('button',name='继续编辑',exact=True).click()
        expect(field).to_have_value('6.5')
        expect(scene).to_have_attribute('data-revision',saved['head_revision_id'])
        attempts, receipts = [], []
        def lost_acceptance(route):
            if route.request.method != 'POST':
                route.continue_(); return
            attempts.append(route.request.post_data_json)
            actual = route.fetch()
            assert actual.status == 202, actual.text()
            receipts.append(actual.json())
            if len(attempts) == 1:
                # Server really accepted this operation. Only delivery is lost.
                route.abort('connectionreset')
            else:
                route.fulfill(response=actual)
        page.route('**/api/documents/*/operations',lost_acceptance)
        tree.get_by_role('button',name='提交参数变更',exact=True).click()
        retry = tree.get_by_role('button',name='核对并重试同一请求',exact=True)
        expect(retry).to_be_visible()
        expect(field).to_have_value('6.5')
        retry.click()
        expect(tree.get_by_role('button',name='已提交候选计算',exact=True)).to_be_visible()
        assert len(attempts) == len(receipts) == 2
        assert attempts[0] == attempts[1], 'retry changed accepted payload or identity'
        assert receipts[0]['workflow_run_id'] == receipts[1]['workflow_run_id']
        workflow = receipts[1]['workflow_run_id']
        page.unroute('**/api/documents/*/operations',lost_acceptance)
    else:
        workflow = None
    if not faults:
        with page.expect_response(lambda r:r.url.endswith('/operations') and r.request.method=='POST') as response:
            tree.get_by_role('button',name='提交参数变更',exact=True).click()
        assert response.value.status==202,response.value.text()
        workflow=response.value.json()['workflow_run_id']
    result=terminal(client,page,workflow)
    if result['status']=='waiting_confirmation':
        save(page,'manual-plan-confirmation')
        tree.get_by_role('region',name='参数执行计划确认',exact=True).get_by_role('button',name='确认并继续',exact=True).click()
        # Wait for the persisted confirmation to be consumed before polling.
        expect(page.get_by_test_id('drawer-task-state')).to_have_attribute('data-task-phase','running',timeout=30000)
        result=terminal(client,page,workflow)
    assert result['status']=='succeeded',result.get('error_message')
    tree.get_by_role('button',name='查看变更 / 应用修改',exact=True).click()
    dialog=page.get_by_role('dialog',name='变更审查',exact=True)
    dialog.get_by_role('textbox',name='审查意见',exact=True).fill('核验本次孔径参数及几何检查，不代表实物适配。')
    expect(dialog.get_by_role('button',name='接受变更',exact=True)).to_have_count(0)
    expect(dialog.get_by_role('button',name='提交版本',exact=True)).to_have_count(0)
    dialog.get_by_role('button',name='应用修改',exact=True).click()
    expect(dialog.get_by_text('审查状态：committed',exact=True)).to_be_visible(timeout=30000)
    dialog.get_by_role('button',name='关闭',exact=True).last.click()
    expect(field).to_be_enabled(timeout=30000)
    expect(field).to_have_value('6.5')
    expect(tree.get_by_text('已保存 · v2，可继续编辑',exact=True)).to_be_visible()
    assert page.evaluate('window.__taskStateCanvas === document.querySelector("[data-testid=document-scene] canvas")')
    final=read(client,'/api/documents/'+fixture['document_id'])
    if faults:
        other_current=read(client,'/api/documents/'+other['document_id'])
        assert other_current['head_revision_id'] == other['base_revision_id']
        assert other_current['state_version'] == 0
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
        'unknown_acceptance_same_request':faults,'panel_scope_and_draft_guard':faults,
        'unchanged_other_parameters':True,'draft_and_selection_survive_collapse':True,'canvas_not_remounted':True,
        'final_generation':final['state_version'],'artifact_sha256':hashes}


def main():
    OUT.mkdir(parents=True,exist_ok=False);OUT.chmod(0o700)
    mode=sys.argv[1] if len(sys.argv)>1 else 'candidate'
    errors=[]
    with httpx.Client(base_url=API,timeout=60,headers={'Authorization':'Bearer '+PRIVATE['owner']['token']}) as client, sync_playwright() as pw:
        auth=client.post('/api/auth/login/password',json={k:PRIVATE['owner'][k] for k in ('phone','password')});auth.raise_for_status()
        client.headers['Authorization']='Bearer '+auth.json()['token']
        browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        context=browser.new_context(viewport={'width':1440,'height':1000})
        context.tracing.start(screenshots=True,snapshots=True)
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            login(page)
            report=({'generation':generation,'geometry':geometry_intake,'candidate':candidate}[mode])(page,client)
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
