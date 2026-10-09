"""Actual import/edit/check/reopen and UI regressions, without HTTP substitutions.

Connection failures use a real offline browser; no fake CAD or provider runs.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from runtime_fixture import run_native_seed

API = os.environ['CAD_NATIVE_E2E_URL']
WEB = os.environ['CAD_NATIVE_E2E_WEB']
ROOT = Path(__file__).resolve().parents[3]
OUT = Path(os.environ['CAD_PRODUCT_BROWSER_REPORT'])
PRIVATE = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())


CONTINUE = '''import asyncio,json,sys
from uuid import UUID
from app.domain.identity import user_principal
from app.models.workflow_requests import OperationContextV1
from app.workflows.temporal import FreeCADStructuredModificationV1
from app.services.durable_submission import submit_durable_workflow
from app.services.cloud_documents import checkpoint
from app.db import close_database
async def main():
    value=json.loads(sys.argv[2]);principal=user_principal(sys.argv[1])
    source=await checkpoint(principal,UUID(value['document_id']),UUID(value['source_revision']))
    ctx=OperationContextV1(rule='explicit_parameter_edit',source_channel='rest',requested_operation='modify',resolved_operation='modify',
        submission_modeling_backend='freecad',base_revision_id=UUID(value['base_revision']),base_source_kind='fcstd_artifact',
        base_source_id=UUID(source['fcstd']['artifact_id']),base_source_sha256=source['fcstd']['sha256'],source_candidate_revision_id=UUID(value['source_revision']))
    modification=FreeCADStructuredModificationV1(expected_state_sha256=source['parameter_state_sha256'],
        parameter_updates=[{'parameter_id':'Pad.Length','value':7}])
    result=await submit_durable_workflow(principal,project_id=UUID(value['project_id']),branch_id=UUID(value['document_id']),
        expected_base_revision_id=UUID(value['base_revision']),expected_state_version=value['state_version'],idempotency_key=value['key'],
        operation='modify',objective='继续修改已请求修改的候选，保留原候选几何',output_formats=['step','stl'],
        modeling_backend='freecad',operation_context=ctx,structured_modification=modification,
        manufacturing_profile={'process':'fdm','material':'PLA'})
    print('CONTINUATION='+str(result.workflow_run_id))
    await close_database()
asyncio.run(main())
'''

SVG_EXECUTE='''import asyncio,json,sys
from app.domain.identity import user_principal
from app.services.durable_submission import ensure_workspace_identity
from app.workflows.temporal import start_mcad_workflow,McadExecutionRequest,McadOutputRequest
from app.db import close_database
async def main():
    owner=user_principal(sys.argv[1])
    workspace=await ensure_workspace_identity(owner,session_id=sys.argv[2],panel_id=sys.argv[3],title=sys.argv[4],user_id=sys.argv[1])
    source="import cadquery as cq\\nresult=cq.Workplane('XY').box(40,30,1)\\ncq.exporters.export(result,'/sandbox/output/result.svg')\\n"
    primary=McadExecutionRequest(step_key='svg-projection',kind='mcad_model',operation='execute',source_code=source,
        outputs=(McadOutputRequest(name='svg',media_type='image/svg+xml'),))
    workflow,handle=await start_mcad_workflow(tenant_id=owner.tenant_id,project_id=workspace.project_id,principal_id=owner.principal_id,
        branch_id=workspace.branch_id,expected_base_revision_id=workspace.head_revision_id,kind='mcad.execute',
        idempotency_key='svg-'+sys.argv[2],primary=primary,objective='Generate an actual 40 x 30 mm plate projection',
        require_confirmation=False,commit_after_confirmation=False)
    result=await handle.result()
    assert result['status']=='succeeded',result
    print('SVG_PROJECTION='+json.dumps({'workflow_run_id':str(workflow),'change_set_id':result['change_set_id'],
        'document_id':str(workspace.branch_id),'title':sys.argv[4]}))
    await close_database()
asyncio.run(main())
'''


def run():
    OUT.mkdir(parents=True, exist_ok=False)
    OUT.chmod(0o700)
    kernel = OUT / 'kernel'
    for folder in ('input', 'output'):
        (kernel / folder).mkdir(parents=True)
        (kernel / folder).chmod(0o777)
    command = [os.getenv('SANDBOX_COMMAND', 'docker'), 'run', '--rm', '--network', 'none', '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,size=2g',
        '-v', str(kernel/'input')+':/sandbox/input:rw', '-v', str(kernel/'output')+':/sandbox/output:rw',
        '-v', str(ROOT/'backend/tests/e2e')+':/tests:ro', '--entrypoint', '/opt/freecad/bin/FreeCADCmd',
        os.environ['SANDBOX_IMAGE'], '-c', "exec(compile(open('/tests/freecad_product_acceptance_contract.py').read(), '/tests/freecad_product_acceptance_contract.py', 'exec'))"]
    with (OUT/'kernel.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    log = (OUT/'kernel.log').read_text()
    assert 'CAD_PRODUCT_ACCEPTANCE_CONTRACT=' in log and 'Traceback (most recent call last)' not in log
    records = []
    with httpx.Client(base_url=API, headers={'Authorization':'Bearer '+PRIVATE['owner']['token']}, timeout=60, trust_env=False) as api:
        def call(method, path, **kwargs):
            response = api.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        def terminal(workflow):
            for _ in range(240):
                snapshot = call('GET', f'/api/tasks/{workflow}/snapshot')
                if snapshot['status'] == 'waiting_confirmation':
                    call('POST', f'/api/tasks/{workflow}/confirmation', json={'accepted':True})
                elif snapshot['status'] in {'succeeded','failed','cancelled','timed_out'}:
                    return snapshot
                time.sleep(.5)
            raise AssertionError('Native task did not reach a terminal state: '+workflow)

        def commit(change):
            note = {'note':'Real product regression: native geometry and frozen source identity checked.'}
            call('POST', f'/api/change-sets/{change}/accept', json=note)
            call('POST', f'/api/change-sets/{change}/commit', json=note)

        def import_file(name):
            data = {'session_id':str(uuid4()), 'panel_id':str(uuid4()), 'idempotency_key':str(uuid4()),
                'manufacturing_profile':json.dumps({'process':'fdm','material':'PLA'})}
            content = (kernel/'input'/name).read_bytes()
            response = api.post('/api/documents/imports', data=data, files={'file':(name,content)})
            assert response.status_code == 202, response.text
            receipt = response.json()
            task = terminal(receipt['workflow_run_id'])
            (OUT/(name+'-task.json')).write_text(json.dumps(task,ensure_ascii=False,indent=2))
            assert task['status']=='succeeded', (task.get('error_code'),task.get('error_message'))
            commit(task['change_set']['id'])
            same = call('POST','/api/documents/imports',data=data,files={'file':(name,content)})
            assert same['workflow_run_id']==receipt['workflow_run_id']
            recovered = call('GET','/api/documents/imports/receipt', params={k:data[k] for k in ('session_id','panel_id','idempotency_key')})
            assert recovered['workflow_run_id']==receipt['workflow_run_id']
            assert api.post('/api/documents/imports',data={**data,'idempotency_key':str(uuid4())},files={'file':(name,content)}).status_code==422
            return receipt, call('GET','/api/documents/'+receipt['branch_id']), data

        fcstd, doc, fcstd_request = import_file('baseline.FCStd')
        step, step_doc, _ = import_file('baseline.step')
        assert any(f['type']=='Sketcher::SketchObject' for f in doc['features'])
        assert not any(f['type']=='Sketcher::SketchObject' for f in step_doc['features'])
        path = '/api/documents/'+doc['document_id']
        with httpx.Client(base_url=API,headers={'Authorization':'Bearer '+PRIVATE['editor']['token']},trust_env=False) as other:
            assert other.get(path).status_code in {403,404}
            assert other.get('/api/analyze/tasks/'+fcstd['workflow_run_id']).status_code in {403,404}
            assert other.get('/api/documents/imports/receipt',params={k:fcstd_request[k] for k in ('session_id','panel_id','idempotency_key')}).status_code==404
        assert httpx.get(API+path,trust_env=False).status_code==401
        records.append('real_fcstd_step_import_replay_owned_receipt_and_permissions')

        update = {'action':'parameters.update','expected_base_revision_id':doc['head_revision_id'],
            'expected_state_version':doc['state_version'],'idempotency_key':str(uuid4()),
            'modification':{'expected_state_sha256':doc['parameter_state_sha256'],
                'parameter_updates':[{'parameter_id':'Pad.Length','value':6}]}}
        first = call('POST',path+'/operations',json=update)
        first_task = terminal(first['workflow_run_id'])
        assert first_task['status']=='succeeded',first_task.get('error_message')
        assert first_task['request_payload']['manufacturing_profile']['process']=='fdm'
        first_change = call('GET','/api/change-sets/'+first_task['change_set']['id'])
        call('POST','/api/change-sets/'+first_change['id']+'/request-change',json={'note':'继续保留候选中的拉伸修改，将长度调整为 7 mm。'})
        assert call('GET',path)['head_revision_id']==doc['head_revision_id']
        value = {'document_id':doc['document_id'],'project_id':doc['project_id'],'base_revision':doc['head_revision_id'],
            'state_version':doc['state_version'],'source_revision':first_change['candidate_revision_id'],'key':str(uuid4())}
        result = run_native_seed(CONTINUE,[PRIVATE['owner']['user']['id'],json.dumps(value)])
        (OUT/'continuation.log').write_text(result.stdout+result.stderr)
        assert result.returncode==0,'candidate continuation failed; see retained log'
        continued = next(line.split('=',1)[1] for line in result.stdout.splitlines() if line.startswith('CONTINUATION='))
        second_task = terminal(continued)
        assert second_task['status']=='succeeded',second_task.get('error_message')
        second_change = call('GET','/api/change-sets/'+second_task['change_set']['id'])
        assert second_change['base_revision_id']==doc['head_revision_id']
        assert second_task['request_payload']['operation_context']['source_candidate_revision_id']==first_change['candidate_revision_id']
        assert call('GET',path)['head_revision_id']==doc['head_revision_id']
        commit(second_change['id'])
        saved = call('GET',path)
        viewed=call('GET',path+'/revisions/'+saved['head_revision_id'])
        assert viewed['manufacturing_profile']['process']=='fdm' and viewed['snapshot']['result']['manufacturing_profile']['material']=='PLA'
        assert saved['state_version']==doc['state_version']+1
        assert next(p['value'] for f in saved['features'] for p in f['parameters'] if p['id']=='Pad.Length')==7
        assert api.post(path+'/operations',json={**update,'idempotency_key':str(uuid4())}).status_code==409
        again = run_native_seed(CONTINUE,[PRIVATE['owner']['user']['id'],json.dumps(value)])
        assert again.returncode==0 and 'CONTINUATION='+continued in again.stdout
        records.append('candidate_a_to_b_native_source_saved_head_cas_idempotency_and_stale_rejection')

        measurement = {'expected_revision_id':saved['head_revision_id'],'expected_state_version':saved['state_version'],
            'idempotency_key':str(uuid4()),'task':{'kind':'native_measure','measurement':'volume','component_name':'Body','selectors':[]}}
        measured = call('POST',path+'/engineering',json=measurement)
        assert terminal(measured['workflow_run_id'])['status']=='succeeded'
        evidence = call('GET',path+'/engineering/'+measured['workflow_run_id'])
        assert evidence['source_revision_id']==saved['head_revision_id'] and abs(evidence['report']['value']-560)<1e-6
        check = api.post('/api/analyze/'+continued,json={'asynchronous':True,'source_revision_id':saved['head_revision_id']})
        assert check.status_code==202 and check.headers['X-Workflow-Run-ID']==check.json()['workflow_run_id'],check.text
        check_id = check.json()['workflow_run_id']
        assert terminal(check_id)['status']=='succeeded'
        analysis = call('GET','/api/analyze/tasks/'+check_id)
        assert analysis['source_revision_id']==saved['head_revision_id'] and analysis['analysis']['process']=='fdm'
        assert analysis['analysis']['evaluation_status'] in {'passed','warning','failed','indeterminate'}
        assert analysis['analysis']['evaluated_rules'] and all(rule['process']=='FDM' for rule in analysis['analysis']['evaluated_rules'])
        if analysis['analysis']['evaluation_status']=='indeterminate':assert analysis['analysis']['design_score'] is None
        assert call('GET',path)['head_revision_id']==saved['head_revision_id']
        records.append('native_volume_async_dfm_frozen_profile_and_no_model_mutation')
        # A real redundant relationship fails on a saved, editable sketch.
        # Recovery reads that valid baseline; diagnostic bytes are never saved.
        (kernel/'input'/'takeover.FCStd').write_bytes((kernel/'input'/'baseline.FCStd').read_bytes())
        _, takeover_doc, _ = import_file('takeover.FCStd')
        takeover_path='/api/documents/'+takeover_doc['document_id']
        sketch=next(feature for feature in takeover_doc['features'] if feature['type']=='Sketcher::SketchObject')
        bad_edit={'action':'native.update','expected_base_revision_id':takeover_doc['head_revision_id'],
            'expected_state_version':takeover_doc['state_version'],'idempotency_key':str(uuid4()),
            'modification':{'expected_state_sha256':takeover_doc['parameter_state_sha256'],'native_edits':[
                {'action':'sketch.patch_relations','args':{'sketch':sketch['kernel_name'],
                    'expected_constraints_sha256':sketch['sketch_constraints_sha256'],
                    'changes':[{'action':'add','logical_id':'relation_'+uuid4().hex,
                        'constraint':{'sketch':sketch['kernel_name'],'kind':'horizontal','first':{'geometry_index':0}}}]}}]}}
        broken=call('POST',takeover_path+'/operations',json=bad_edit)
        assert terminal(broken['workflow_run_id'])['status']=='failed'
        diagnostic=call('GET','/api/tasks/'+broken['workflow_run_id']+'/diagnostic')
        assert diagnostic['snapshot']['valid_checkpoint'] is False
        takeover_url='/api/tasks/'+broken['workflow_run_id']+'/takeover-baseline'
        baseline=call('GET',takeover_url)
        assert baseline['mode']=='valid_baseline' and baseline['revision_id']==takeover_doc['head_revision_id']
        assert baseline['sketches'][0]['feature_id']==sketch['id']
        lease=call('POST',takeover_path+'/leases',json={'feature_id':sketch['id'],'client_id':str(uuid4()),
            'revision_id':takeover_doc['head_revision_id']})
        leased_response=api.get(takeover_url)
        assert leased_response.status_code==422 and '编辑租约' in leased_response.json()['detail']
        api.delete(takeover_path+'/leases/'+lease['token']).raise_for_status()
        assert call('GET',takeover_url)['revision_id']==takeover_doc['head_revision_id']
        with sync_playwright() as pw:
            recovery_browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            recovery_context=recovery_browser.new_context(viewport={'width':1440,'height':1000})
            recovery_context.tracing.start(screenshots=True,snapshots=True)
            recovery_page=recovery_context.new_page();recovery_errors=[]
            recovery_page.on('pageerror',lambda error:recovery_errors.append(str(error)))
            try:
                recovery_page.goto(WEB,wait_until='domcontentloaded')
                recovery_page.locator('#account').fill(PRIVATE['owner']['phone'])
                recovery_page.locator('#password').fill(PRIVATE['owner']['password'])
                recovery_page.locator('button[type=submit]').click()
                recovery_page.get_by_role('button',name='导入 takeover.FCStd',exact=False).first.click()
                failed_card=recovery_page.get_by_test_id('authoritative-task')
                expect(failed_card).to_have_attribute('data-task-phase','failed')
                expect(failed_card).to_have_attribute('data-task-id',broken['workflow_run_id'])
                failed_card.get_by_role('button',name='查看失败草图与求解诊断',exact=True).click()
                failure_dialog=recovery_page.get_by_role('dialog',name='失败现场诊断',exact=True)
                expect(failure_dialog.get_by_role('group',name='失败草图几何',exact=True)).to_be_visible()
                failure_dialog.get_by_role('button',name='接管有效基线并修正参数',exact=True).click()
                expect(failure_dialog).to_be_hidden()
                editor=recovery_page.get_by_test_id('sketch-editor')
                expect(editor.get_by_test_id('sketch-preview')).to_have_attribute('data-revision',takeover_doc['head_revision_id'])
                editor.get_by_role('button',name='打开大画布草图',exact=True).click()
                expanded=recovery_page.get_by_role('dialog',name='原生草图画布',exact=True)
                expect(expanded.get_by_test_id('sketch-preview')).to_be_visible()
                assert expanded.get_by_test_id('sketch-preview').bounding_box()['height']>=400
                recovery_page.screenshot(path=str(OUT/'takeover-valid-baseline.png'))
                assert not recovery_errors,recovery_errors
            except BaseException:
                recovery_page.screenshot(path=str(OUT/'takeover-failure.png'))
                (OUT/'takeover-failure.txt').write_text(recovery_page.locator('body').inner_text())
                raise
            finally:
                recovery_context.tracing.stop(path=str(OUT/'takeover-trace.zip'));recovery_browser.close()
        dimensions=call('POST',takeover_path+'/inspect',json={'revision_id':takeover_doc['head_revision_id'],
            'query':{'objects':[sketch['kernel_name']],'fields':['constraints'],'offset':0,'limit':32}})
        width=next(item for item in dimensions['objects'][0]['constraints']['items'] if item['type']=='Distance' and item['value']==10)
        corrected={**bad_edit,'idempotency_key':str(uuid4()),'modification':{
            'expected_state_sha256':takeover_doc['parameter_state_sha256'],'native_edits':[
                {'action':'sketch.set_constraint','args':{'sketch':sketch['kernel_name'],'constraint_index':width['index'],
                    'expected_type':'Distance','value_mm':12}}]}}
        recovered=call('POST',takeover_path+'/operations',json=corrected)
        recovered_task=terminal(recovered['workflow_run_id'])
        assert recovered_task['status']=='succeeded',recovered_task.get('error_message')
        assert call('GET',takeover_path)['head_revision_id']==takeover_doc['head_revision_id']
        commit(recovered_task['change_set']['id'])
        assert call('GET',takeover_path)['head_revision_id']!=takeover_doc['head_revision_id']
        stale_response=api.get(takeover_url)
        assert stale_response.status_code==422 and '原基线已变化' in stale_response.json()['detail']
        records.append('real_failed_sketch_stopped_takeover_lease_guard_native_recovery_commit_and_stale_rejection')
        svg_result=run_native_seed(SVG_EXECUTE,[PRIVATE['owner']['user']['id'],str(uuid4()),str(uuid4()),'SVG projection '+uuid4().hex[:8]])
        (OUT/'svg-projection.log').write_text(svg_result.stdout+svg_result.stderr)
        assert svg_result.returncode==0,'actual CAD projection failed; see svg-projection.log'
        svg_fixture=json.loads(next(line.split('=',1)[1] for line in svg_result.stdout.splitlines() if line.startswith('SVG_PROJECTION=')))
        commit(svg_fixture['change_set_id'])
        (OUT/'api-fixture.json').write_text(json.dumps({'cases':records,'saved':saved,'base':doc,'step':step_doc,'svg':svg_fixture},ensure_ascii=False,indent=2))

        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            ctx=browser.new_context(viewport={'width':390,'height':844})
            ctx.tracing.start(screenshots=True,snapshots=True)
            page=ctx.new_page(); errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            try:
                page.goto(WEB,wait_until='domcontentloaded')
                password=page.locator('#password')
                normal_border=password.evaluate('e=>getComputedStyle(e).borderColor')
                password.fill('a-long-visible-password-for-padding-check')
                expect(password).to_have_css('border-color',normal_border)
                expect(password).to_have_css('box-shadow','none')
                expect(password).to_have_css('outline-width','2px')
                page.get_by_role('button',name='显示密码',exact=True).click()
                assert password.evaluate('e=>parseFloat(getComputedStyle(e).paddingRight)')>=48
                assert page.locator('button[type=submit]').bounding_box()['height']>=44
                page.set_viewport_size({'width':1440,'height':1000})
                assert page.locator('button[type=submit]').bounding_box()['height']>=44
                assert page.locator('form > [aria-live]').evaluate('e=>getComputedStyle(e).display')=='none'
                page.set_viewport_size({'width':390,'height':844})
                page.get_by_role('tab',name='邀请码注册',exact=True).click()
                page.locator('#confirm-password').fill('different-value');page.locator('#confirm-password').focus()
                invalid=page.locator('#confirm-password')
                expect(invalid).to_have_attribute('aria-invalid','true')
                assert invalid.evaluate(r'e=>{const [r,g,b]=getComputedStyle(e).borderColor.match(/\d+/g).map(Number);return r>120 && g<100 && b<100}')
                expect(invalid).to_have_css('outline-color',invalid.evaluate('e=>getComputedStyle(e).borderColor'))
                page.get_by_role('tab',name='登录',exact=True).click()
                page.locator('#account').fill(PRIVATE['owner']['phone']);password.fill(PRIVATE['owner']['password'])
                page.locator('button[type=submit]').click()
                page.set_viewport_size({'width':1440,'height':1000})
                page.get_by_role('button',name='导入 baseline.FCStd',exact=False).first.click()
                scene=page.get_by_test_id('document-scene')
                expect(scene).to_have_attribute('data-revision',saved['head_revision_id'],timeout=90000)
                expect(page.get_by_test_id('view-identity')).to_have_css('background-color','rgb(255, 255, 255)')
                records.append('actual_auth_error_padding_control_sizes_and_saved_reopen')
                for width in [1440,1024,768,390,320]:
                    page.set_viewport_size({'width':width,'height':900})
                    expect(scene.locator('canvas')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
                    source=page.locator('.ww-version-source > summary');source.click()
                    assert source.evaluate('e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}'),width
                    source.click()
                    if width<=760:
                        assert scene.get_by_role('combobox',name='网格精度',exact=True).bounding_box()['height']>=44
                        page.get_by_role('button',name='打开项目导航',exact=True).click()
                        navigation=page.get_by_role('button',name='新建设计',exact=True)
                        expect(navigation).to_be_visible()
                        page.wait_for_function('e=>{const r=e.getBoundingClientRect();return r.x>=0 && e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}',arg=navigation.element_handle(),timeout=5000)
                        page.get_by_role('button',name='关闭项目导航',exact=True).last.click()
                        expect(navigation).to_be_hidden()
                    page.screenshot(path=str(OUT/f'workspace-{width}.png'))
                records.append('responsive_resize_source_lod_navigation_hit_targets')
                page.set_viewport_size({'width':1440,'height':1000})
                scene.get_by_role('combobox',name='标准视图',exact=True).select_option('front')
                scene.get_by_role('button',name='正交',exact=True).click()
                expect(scene.get_by_role('button',name='正交',exact=True)).to_have_attribute('aria-pressed','true')
                scene.get_by_role('button',name='正交',exact=True).click()
                page.get_by_role('button',name='属性',exact=True).click()
                tree=page.locator('[data-testid=cloud-document-panel]:visible')
                tree.get_by_role('treeitem',name='Pad',exact=True).click()
                field=tree.get_by_role('spinbutton',name='Pad.Length',exact=True);field.fill('8')
                tree.get_by_role('button',name='撤销草稿',exact=True).click();expect(field).to_have_value('7')
                tree.get_by_role('button',name='重做草稿',exact=True).click();expect(field).to_have_value('8')
                page.get_by_role('button',name='版本',exact=True).click()
                page.locator(f'[data-revision-id="{doc["head_revision_id"]}"]').get_by_role('button',name='查看此版本',exact=True).click()
                draft=page.get_by_role('dialog',name='保留编辑草稿',exact=True)
                expect(draft).to_be_visible()
                for _ in range(12):
                    page.keyboard.press('Tab')
                    assert draft.evaluate('e=>e.contains(document.activeElement)')
                draft.get_by_role('button',name='继续编辑',exact=True).click()
                page.get_by_role('button',name='属性',exact=True).click()
                expect(field).to_have_value('8')
                tree.get_by_role('button',name='撤销草稿',exact=True).click()
                kernel_properties=tree.get_by_role('button',name='内核属性',exact=True);kernel_properties.click()
                expect(kernel_properties).to_have_attribute('aria-pressed','true')
                page.mouse.move(0,0)
                expect(kernel_properties).to_have_css('background-color','rgb(242, 237, 255)')
                page.get_by_role('button',name='返回 Agent',exact=True).click()
                records.append('scoped_undo_redo_draft_focus_trap_and_kernel_selected_state')
                page.get_by_role('button',name='检查',exact=True).click()
                checks=page.locator('.ww-inspector-pane')
                expect(checks.get_by_role('combobox',name='检查工艺',exact=True)).to_have_value('fdm')
                checks.get_by_role('combobox',name='检查工艺',exact=True).select_option('cnc')
                page.get_by_role('button',name='返回 Agent',exact=True).click()
                page.get_by_role('button',name='检查',exact=True).click()
                expect(checks.get_by_role('combobox',name='检查工艺',exact=True)).to_have_value('cnc')
                checks.get_by_role('combobox',name='检查工艺',exact=True).select_option('')
                expect(checks.get_by_role('combobox',name='检查工艺',exact=True)).to_have_value('')
                checks.get_by_role('combobox',name='检查工艺',exact=True).select_option('fdm')
                with page.expect_response(lambda response:'/api/analyze/' in response.url and response.request.method=='POST') as checking:
                    checks.get_by_role('button',name='运行工程检查',exact=True).click()
                assert checking.value.status==202,checking.value.text()
                browser_check_id=checking.value.json()['workflow_run_id']
                page.reload(wait_until='domcontentloaded')
                expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision',saved['head_revision_id'],timeout=90000)
                page.get_by_role('button',name='检查',exact=True).click()
                checked=page.locator('.ww-inspector-pane')
                expect(checked.get_by_role('button',name='运行工程检查',exact=True)).to_be_enabled(timeout=90000)
                assert call('GET','/api/analyze/tasks/'+browser_check_id)['analysis']['source_revision_id']==saved['head_revision_id']
                assert call('GET',path)['head_revision_id']==saved['head_revision_id']
                page.get_by_role('button',name='返回 Agent',exact=True).click()
                page.get_by_role('button',name='版本',exact=True).click()
                page.locator(f'[data-revision-id="{doc["head_revision_id"]}"]').get_by_role('button',name='查看此版本',exact=True).click()
                expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision',doc['head_revision_id'],timeout=90000)
                page.get_by_role('button',name='图形对比保存基线',exact=True).click()
                comparison=page.get_by_role('dialog',name='修订图形对比',exact=True)
                expect(comparison.locator('[data-testid=document-scene] canvas')).to_have_count(2,timeout=90000)
                comparison.get_by_role('button',name='关闭',exact=True).click()
                page.get_by_role('button',name='查看已提交版本',exact=True).click()
                expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision',saved['head_revision_id'],timeout=90000)
                page.get_by_role('button',name='返回 Agent',exact=True).click()
                records.append('actual_async_check_reload_and_readonly_graphical_revision_comparison')
                page.locator('.workspace-header').get_by_role('button',name='设置',exact=True).click()
                settings=page.get_by_role('dialog',name='设置',exact=True)
                expect(settings.locator('.ww-rule').first).to_be_visible()
                checkbox=settings.get_by_role('checkbox').first
                assert checkbox.evaluate('e=>!!e.closest("label") && getComputedStyle(e).accentColor!=="auto"')
                settings.get_by_role('tab',name='工艺知识图谱',exact=True).click()
                settings.get_by_role('button',name='供应商',exact=True).click()
                settings.get_by_role('button',name='+ 添加供应商',exact=True).click()
                supplier=page.get_by_role('dialog',name='添加供应商',exact=True)
                expect(supplier).to_be_visible()
                for _ in range(8):
                    page.keyboard.press('Tab');assert supplier.evaluate('e=>e.contains(document.activeElement)')
                supplier.get_by_role('button',name='取消',exact=True).click()
                assert page.locator('#root').evaluate('e=>e.inert')
                settings.get_by_role('button',name='关闭设置',exact=True).click()
                assert not page.locator('#root').evaluate('e=>e.inert')
                # Real transport failure remains an error, then retries against
                # the restored live backend, without synthesising a response.
                ctx.set_offline(True)
                page.locator('.workspace-header').get_by_role('button',name='设置',exact=True).click()
                settings=page.get_by_role('dialog',name='设置',exact=True)
                settings.get_by_role('tab',name='DFM 规则',exact=True).click()
                expect(settings.get_by_role('alert')).to_be_visible()
                ctx.set_offline(False);settings.get_by_role('button',name='重试',exact=True).click()
                expect(settings.locator('.ww-rule').first).to_be_visible()
                settings.get_by_role('button',name='关闭设置',exact=True).click()
                ctx.set_offline(True)
                page.locator('.workspace-header').get_by_role('button',name='设置',exact=True).click()
                settings=page.get_by_role('dialog',name='设置',exact=True)
                settings.get_by_role('tab',name='工艺知识图谱',exact=True).click()
                expect(settings.get_by_role('alert')).to_be_visible()
                expect(settings.get_by_text('无支持材料记录',exact=True)).to_have_count(0)
                ctx.set_offline(False);settings.get_by_role('button',name='重试',exact=True).click()
                expect(settings.get_by_role('button',name='FDM',exact=True)).to_be_visible()
                ctx.set_offline(True);settings.get_by_role('button',name='供应商',exact=True).click()
                expect(settings.get_by_role('alert')).to_be_visible()
                ctx.set_offline(False);settings.get_by_role('button',name='重试',exact=True).click()
                expect(settings.get_by_role('alert')).to_have_count(0)
                settings.get_by_role('button',name='智能推荐',exact=True).click()
                ctx.set_offline(True);settings.get_by_role('button',name='推荐',exact=True).click()
                expect(settings.get_by_role('alert')).to_be_visible()
                ctx.set_offline(False);settings.get_by_role('button',name='重试',exact=True).click()
                expect(settings.get_by_role('alert')).to_have_count(0)
                settings.get_by_role('button',name='关闭设置',exact=True).click()
                records.append('direct_settings_dfm_accessible_label_nested_modal_and_real_failure_retry')
                page.locator('.workspace-header').get_by_role('button',name='切换为英文',exact=True).click()
                expect(scene.get_by_role('combobox',name='Mesh detail',exact=True)).to_be_visible()
                expect(scene.get_by_role('combobox',name='Standard view',exact=True)).to_be_visible()
                expect(page.locator('.ww-version-source > summary')).to_have_text('Revision source')
                expect(page.get_by_test_id('view-identity')).to_contain_text('Saved revision')
                expect(page.locator('.ww-agent-panel').get_by_role('heading',name='User requirements',exact=True)).to_be_visible()
                expect(page.locator('.ww-agent-panel').get_by_role('textbox',name='Ask Agent',exact=True)).to_be_visible()
                page.locator('.workspace-header').get_by_role('button',name='Switch to Chinese',exact=True).click()
                records.append('native_chrome_english_identity_and_precision')
                page.get_by_role('button',name=svg_fixture['title'],exact=False).first.click()
                svg_view=page.get_by_test_id('svg-viewport')
                expect(svg_view.get_by_role('img',name='二维工程图预览',exact=True)).to_be_visible()
                page.wait_for_function('e=>Number(e.dataset.zoom)>0 && e.querySelector("img").naturalWidth>0',arg=svg_view.element_handle())
                fit=float(svg_view.get_attribute('data-zoom'))
                page.get_by_role('button',name='放大二维图',exact=True).click();page.get_by_role('button',name='放大二维图',exact=True).click()
                expect(svg_view).not_to_have_attribute('data-zoom',str(fit))
                page.get_by_role('button',name='适应视图',exact=True).click()
                page.wait_for_function('(v)=>Math.abs(Number(v.element.dataset.zoom)-v.expected)<1e-6',arg={'element':svg_view.element_handle(),'expected':fit})
                page.set_viewport_size({'width':320,'height':900})
                page.wait_for_function('e=>e.querySelector("img").getBoundingClientRect().width<=e.clientWidth',arg=svg_view.element_handle())
                records.append('actual_cad_svg_projection_zoom_and_fit_resize')
                page.goto(WEB+'/share?document='+saved['document_id'],wait_until='domcontentloaded')
                expect(page.get_by_test_id('cloud-document-panel')).to_be_visible(timeout=30000)
                for width in [1440,768,390,768]:
                    page.set_viewport_size({'width':width,'height':900})
                    aside=page.locator('main > div > aside')
                    expect(aside).to_be_visible()
                    rect=aside.bounding_box();assert rect['x']+rect['width']<=width+.5
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(OUT/'shared-resize.png'))
                records.append('shared_document_live_resize_and_inspector_visible')
                assert not errors,errors
            except BaseException:
                ctx.set_offline(False)
                page.screenshot(path=str(OUT/'failure.png'))
                (OUT/'failure.txt').write_text(page.locator('body').inner_text())
                raise
            finally:
                ctx.tracing.stop(path=str(OUT/'trace.zip'));browser.close()
        (OUT/'report.json').write_text(json.dumps({'passed':True,'cases':records,'paid_provider_calls':0,
            'source_revision':saved['head_revision_id'],'source_fcstd_sha256':saved['fcstd']['sha256'],
            'real_kernel':True,'real_auth':True,'network_substitution':False},ensure_ascii=False,indent=2))
        print('PRODUCT ACCEPTANCE BROWSER PASSED',flush=True)


if __name__=='__main__':run()
