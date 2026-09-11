"""Reopen a real failed task and verify the error and absence of a candidate.

An expected error code is mandatory so unrelated failures cannot pass this test.
"""
import json
import os
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from cloud_document_acceptance import PRIVATE, call


def main():
    out = Path(os.environ['CAD_FAILED_TASK_REPORT_DIR'])
    expected_code = os.environ['CAD_EXPECTED_FAILURE_CODE']
    submitted = json.loads((out/'browser.json').read_text())
    private = json.loads(PRIVATE.read_text())
    with httpx.Client(headers={'Authorization':'Bearer '+private['owner']['token']}) as client:
        task = call(client,'GET','/api/tasks/'+submitted['workflow_run_id']+'/snapshot')
        assert task['status']=='failed' and task['error_code']==expected_code
        assert task['error_message']
        assert not task['change_set'] and not task['artifacts']
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width':1440,'height':1000})
            errors = []
            page.on('pageerror',lambda e:errors.append(str(e)))
            web = os.environ['CAD_NATIVE_E2E_WEB']
            page.goto(web)
            page.locator('input[type="text"]').fill(private['owner']['phone'])
            page.locator('input[type="password"]').fill(private['owner']['password'])
            page.locator('button[type="submit"]').click()
            expect(page.get_by_role('button',name='新建设计',exact=True)).to_be_visible(timeout=20000)
            page.goto(web+'?document='+submitted['branch_id']+'&workspace='+private['tenant_id'])
            task_button = page.get_by_role('button',name='查看任务',exact=True).first
            expect(task_button).to_be_visible(timeout=20000)
            task_button.click()
            panel = page.get_by_role('region',name='文档任务',exact=True)
            expect(panel.get_by_role('alert')).to_have_text(task['error_message'],timeout=20000)
            expect(panel.get_by_role('button',name='审阅任务变更',exact=True)).to_have_count(0)
            expect(page.get_by_role('button',name='查看变更',exact=True)).to_have_count(0)
            page.screenshot(path=str(out/'failure-reopened.png'))
            assert not errors,errors
            browser.close()
        document = call(client,'GET','/api/documents/'+submitted['branch_id'])
        assert document['state_version']==0 and not document['fcstd']
    evidence = {'workflow_id':task['id'],'document_id':submitted['branch_id'],
        'expected_error_code':expected_code,'actual_error_code':task['error_code'],
        'failure_visible_after_reopen':True,'no_review_button':True,
        'head_unchanged':True,'no_candidate_or_artifacts':True,'page_errors':errors}
    (out/'reopened-failure.json').write_text(json.dumps(evidence,indent=2))
    print('CAD_FAILED_TASK_BROWSER='+json.dumps(evidence),flush=True)


if __name__=='__main__':
    main()
