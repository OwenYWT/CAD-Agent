"""Browser branch comparison, explicit geometry adoption, fork and reviewed commit."""
import json
import os
from pathlib import Path
from acceptance_paths import evidence_path
import time
from uuid import uuid4
from urllib.parse import urlparse, parse_qs

import httpx
from playwright.sync_api import expect, sync_playwright
from cloud_document_acceptance import call, wait_task, commit, accept_in_browser
from cloud_merge_acceptance import modify


def review_in_browser(page, client, task_id):
    deadline=time.monotonic()+240
    while time.monotonic()<deadline:
        task=call(client,'GET','/api/tasks/'+task_id+'/snapshot')
        if task['status']=='waiting_confirmation':
            button=page.get_by_role('button',name='确认并继续',exact=True)
            expect(button).to_be_enabled(timeout=15000);button.click()
        elif task['status'] in ('succeeded','failed','cancelled','timed_out'):
            break
        page.wait_for_timeout(2000)
    assert task['status']=='succeeded',task.get('error_message')
    button=page.get_by_role('button',name='审阅任务变更',exact=True)
    expect(button).to_be_visible(timeout=15000);button.click()
    dialog=page.get_by_role('dialog')
    accept_in_browser(dialog, '已阅读分支变更与检查风险；按明确选择的几何来源提交，并核对提交后的原生模型。')
    expect(dialog.get_by_text('审查状态：accepted',exact=True)).to_be_visible(timeout=15000)
    dialog.get_by_role('button',name='提交版本',exact=True).click()
    expect(dialog.get_by_text('审查状态：committed',exact=True)).to_be_visible(timeout=20000)
    dialog.get_by_role('button',name='关闭',exact=True).last.click()
    expect(page.get_by_role('region',name='文档任务').get_by_role('heading',name='版本已提交',exact=True)).to_be_visible(timeout=10000)


def main():
    private=json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    fixture=json.loads(Path(str(evidence_path('cad-expansion-merge-evidence.json'))).read_text())
    web=os.environ['CAD_NATIVE_E2E_WEB']
    client=httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']})
    path='/api/documents/'+fixture['target_document_id']
    source_path='/api/documents/'+fixture['source_document_id']
    # Recover our interrupted browser fixture, if a previous test navigation
    # lost its response body after the server had durably accepted the fork.
    for branch in call(client,'GET',path+'/branches')['branches']:
        if branch['name'].startswith('browser-fork-') and branch['state_version']==0 and branch['workflow_run_id']:
            commit(client,wait_task(client,branch['workflow_run_id']))
    target,source=call(client,'GET',path),call(client,'GET',source_path)
    if not call(client,'GET',path+'/compare/'+fixture['source_document_id'])['conflicts']:
        length=next(p['value'] for f in target['features'] for p in f['parameters'] if p['id']=='PadA.Length')
        target=modify(client,fixture['target_document_id'],'PadA.Length',length+1)
        source=modify(client,fixture['source_document_id'],'PadA.Length',length+5)
    source_feature=next(f for f in source['features'] if f['kernel_name']=='PadA')
    call(client,'PUT',source_path+'/features/'+source_feature['id']+'/annotation',json={
        'annotation_id':str(uuid4()),'revision_id':source['head_revision_id'],'expected_version':source_feature['annotation_version'],
        'role':'分支独立用途','intent':'来源标注不应覆盖目标'})
    errors,console_errors=[],[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_context(viewport={'width':1440,'height':1000}).new_page()
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('console',lambda msg:console_errors.append(msg.text) if msg.type=='error' else None)
        page.goto(web)
        page.locator('input[type="text"]').fill(private['owner']['phone'])
        page.locator('input[type="password"]').fill(private['owner']['password'])
        page.locator('button[type="submit"]').click()
        expect(page.get_by_role('button',name='新建设计',exact=True)).to_be_visible(timeout=20000)
        page.goto(web+'?document='+fixture['target_document_id']+'&workspace='+fixture['tenant_id'])
        region=page.get_by_role('region',name='文档分支')
        expect(region.get_by_role('combobox',name='比较来源分支')).to_be_visible(timeout=20000)
        region.get_by_role('combobox',name='比较来源分支').select_option(fixture['source_document_id'])
        region.get_by_role('button',name='比较分支差异',exact=True).click()
        comparison=page.get_by_test_id('branch-comparison')
        expect(comparison.get_by_role('button',name='提交合并校验')).to_be_disabled(timeout=20000)
        expect(comparison.get_by_text('来源标注不应覆盖目标',exact=False)).to_have_count(1)
        expect(comparison.get_by_test_id('merge-source-version')).to_contain_text(f"v{source['state_version']} · {source['head_revision_id'][:8]}")
        expect(comparison.get_by_test_id('merge-target-version')).to_contain_text(f"v{target['state_version']} · {target['head_revision_id'][:8]}")
        page.screenshot(path=str(evidence_path('cad-expansion-branch-conflict.png')))
        comparison.get_by_role('combobox',name='合并方式').select_option('source_geometry')
        def advance_and_rollback(document):
            previous = {p['id']:p['value'] for f in document['features'] for p in f['parameters']}
            modified = modify(client,document['document_id'],'PadA.Length',previous['PadA.Length']+1)
            operations = call(client,'GET','/api/documents/'+document['document_id']+'/collaboration')['operations']
            op = next(o for o in operations if o['result_revision_id']==modified['head_revision_id'])
            call(client,'POST','/api/change-sets/'+op['change_set_id']+'/rollback',json={'note':'Restore this isolated ABA test edit after inspecting its actual checkpoint'})
            restored = call(client,'GET','/api/documents/'+document['document_id'])
            assert restored['head_revision_id']==document['head_revision_id'] and restored['state_version']==document['state_version']+2
            return restored
        source = advance_and_rollback(source)
        expect(comparison.get_by_text('来源版本已更新，请重新比较。',exact=True)).to_be_visible(timeout=15000)
        expect(comparison.get_by_role('button',name='提交合并校验')).to_be_disabled()
        region.get_by_role('button',name='比较分支差异',exact=True).click()
        expect(comparison.get_by_test_id('merge-source-version')).to_contain_text(f"v{source['state_version']} · {source['head_revision_id'][:8]}",timeout=15000)
        target = advance_and_rollback(target)
        expect(comparison.get_by_text('目标版本已更新，请重新比较。',exact=True)).to_be_visible(timeout=15000)
        expect(comparison.get_by_role('button',name='提交合并校验')).to_be_disabled()
        region.get_by_role('button',name='比较分支差异',exact=True).click()
        expect(comparison.get_by_test_id('merge-target-version')).to_contain_text(f"v{target['state_version']} · {target['head_revision_id'][:8]}",timeout=15000)
        with page.expect_response(lambda r:r.url.endswith(path+'/merges') and r.request.method=='POST') as submitted:
            comparison.get_by_role('button',name='提交合并校验').click()
        assert submitted.value.status==202,submitted.value.text()
        merge_id=submitted.value.json()['workflow_run_id']
        review_in_browser(page,client,merge_id)
        adopted=call(client,'GET',path)
        params=lambda d:{p['id']:p['value'] for f in d['features'] for p in f['parameters']}
        assert params(adopted)==params(source)
        assert next(f['role'] for f in adopted['features'] if f['kernel_name']=='PadA')==next(f['role'] for f in target['features'] if f['kernel_name']=='PadA')
        expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision',adopted['head_revision_id'],timeout=30000)
        name='browser-fork-'+uuid4().hex[:8]
        region.get_by_role('textbox',name='新分支名称').fill(name)
        with page.expect_response(lambda r:r.url.endswith(path+'/branches') and r.request.method=='POST') as created:
            region.get_by_role('button',name='创建并校验分支').click()
        assert created.value.status==202,created.value.text()
        page.wait_for_url(lambda url:'task=' in str(url) and fixture['target_document_id'] not in str(url),timeout=20000)
        query=parse_qs(urlparse(page.url).query)
        branch={'document_id':query['document'][0],'tenant_id':query['workspace'][0],'workflow_run_id':query['task'][0]}
        persisted=next(b for b in call(client,'GET',path+'/branches')['branches'] if b['name']==name)
        assert persisted['document_id']==branch['document_id'] and persisted['workflow_run_id']==branch['workflow_run_id']
        expect(page).to_have_url(web.rstrip('/')+'/?document='+branch['document_id']+'&workspace='+branch['tenant_id']+'&task='+branch['workflow_run_id'],timeout=15000)
        review_in_browser(page,client,branch['workflow_run_id'])
        viewer=page.get_by_test_id('document-scene')
        final=call(client,'GET','/api/documents/'+branch['document_id'])
        expect(viewer).to_have_attribute('data-revision',final['head_revision_id'],timeout=45000)
        expect(viewer.get_by_text('2 个部件',exact=False)).to_be_visible()
        expect(page.get_by_role('region',name='文档分支').get_by_role('link',name=name,exact=True)).to_have_attribute('aria-current','page')
        assert viewer.locator('canvas').evaluate("c=>!!c.getContext('webgl2')")
        page.screenshot(path=str(evidence_path('cad-expansion-branch-browser.png')))
        assert not errors,errors
        assert not console_errors,console_errors
        Path(str(evidence_path('cad-expansion-branch-browser.json'))).write_text(json.dumps({'merge_workflow_id':merge_id,
            'created_branch':branch,'geometry_adoption_reviewed':True,'target_annotation_preserved':True,
            'fork_created_and_committed_in_browser':True,'source_and_target_aba_disable_stale_merge':True,
            'fresh_comparison_allows_reviewed_merge':True,'page_errors':errors,'console_errors':console_errors},indent=2))
        print('browser comparison, conflict, geometry adoption, fork and review/commit passed',flush=True)
        browser.close()
    client.close()


if __name__=='__main__':
    main()
