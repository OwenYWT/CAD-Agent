"""Real parameter editing against a deployment built without source mounts.

Requires a committed seed_collaboration_document.py fixture and a private owner
session in CAD_NATIVE_E2E_PRIVATE. CAD_COEDIT_REPORT_DIR must be a new directory.
The test performs browser input, real native execution, review, commit, reload,
and authenticated artifact hashing. It does not require a Provider substitution.
"""
import hashlib
import json
import os
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from cloud_branch_browser import review_in_browser
from cloud_document_acceptance import call


def main():
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    out = Path(os.environ['CAD_COEDIT_REPORT_DIR'])
    out.mkdir(parents=True, exist_ok=False)
    web = os.environ['CAD_NATIVE_E2E_WEB']
    path = '/api/documents/' + private['document_id']
    client = httpx.Client(headers={'Authorization':'Bearer ' + private['owner']['token']})
    before = call(client, 'GET', path)
    params = lambda doc: {p['id']:p['value'] for f in doc['features'] for p in f['parameters']}
    target = params(before)['PadA.Length'] + 1
    errors, console_errors = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        context = browser.new_context(viewport={'width':1440,'height':1000})
        context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page()
        page.on('pageerror', lambda error:errors.append(str(error)))
        page.on('console', lambda msg:console_errors.append(msg.text) if msg.type=='error' else None)
        try:
            page.goto(web)
            page.locator('input[type="text"]').fill(private['owner']['phone'])
            page.locator('input[type="password"]').fill(private['owner']['password'])
            page.locator('button[type="submit"]').click()
            expect(page.get_by_role('button',name='新建设计',exact=True)).to_be_visible(timeout=20000)
            page.goto(web + '?document=' + private['document_id'] + '&workspace=' + private['tenant_id'])
            panel = page.get_by_test_id('cloud-document-panel')
            panel.get_by_role('treeitem',name='PadA',exact=True).click()
            expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision',before['head_revision_id'],timeout=90000)
            panel.get_by_role('spinbutton',name='PadA.Length',exact=True).fill(str(target))
            expect(panel.get_by_text('已保留此特征的编辑租约。',exact=False)).to_be_visible(timeout=10000)
            with page.expect_response(lambda response:response.url.endswith(path+'/operations') and response.request.method=='POST') as submitted:
                panel.get_by_role('button',name='提交参数变更',exact=True).click()
            assert submitted.value.status == 202, submitted.value.text()
            workflow_id = submitted.value.json()['workflow_run_id']
            page.reload()
            review_in_browser(page, client, workflow_id)
            after = call(client, 'GET', path)
            assert after['state_version'] == before['state_version'] + 1
            assert after['head_revision_id'] != before['head_revision_id']
            assert params(after) == {**params(before),'PadA.Length':target}
            assert {f['id'] for f in before['features']} == {f['id'] for f in after['features']}
            page.reload()
            panel = page.get_by_test_id('cloud-document-panel')
            panel.get_by_role('treeitem',name='PadA',exact=True).click()
            expect(panel.get_by_role('spinbutton',name='PadA.Length',exact=True)).to_have_value(f'{target:g}',timeout=20000)
            expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision',after['head_revision_id'],timeout=90000)
            hashes = {}
            for kind in ('fcstd','state','mesh'):
                artifact = after[kind]
                response = client.get(os.environ['CAD_NATIVE_E2E_URL'] + artifact['url'],timeout=60)
                response.raise_for_status()
                actual = hashlib.sha256(response.content).hexdigest()
                assert actual == artifact['sha256']
                (out / (kind + '.bin')).write_bytes(response.content)
                hashes[kind] = actual
            assert not errors and not console_errors, (errors, console_errors)
            report = {'status':'passed','document_id':private['document_id'],'workflow_id':workflow_id,
                'before_revision':before['head_revision_id'],'after_revision':after['head_revision_id'],
                'before_generation':before['state_version'],'after_generation':after['state_version'],
                'pad_a_length_mm':target,'pad_b_unchanged':True,'stable_feature_ids':True,
                'refresh_during_task_and_after_commit':True,'artifact_sha256':hashes,
                'page_errors':errors,'console_errors':console_errors}
            (out / 'report.json').write_text(json.dumps(report,indent=2))
            page.screenshot(path=str(out/'committed.png'))
            print('CAD_PACKAGED_BROWSER='+json.dumps(report),flush=True)
        except BaseException:
            page.screenshot(path=str(out/'failure.png'))
            (out/'failure.txt').write_text(page.locator('body').inner_text())
            raise
        finally:
            context.tracing.stop(path=str(out/'trace.zip'))
            browser.close()
            client.close()


if __name__ == '__main__':
    main()
