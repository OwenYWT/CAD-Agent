"""Real server rejections, candidate rejection, stale retry and responsive drafts.

Uses isolated, newly created design documents. All results come from actual
HTTP/WS, PostgreSQL, Temporal, S3 and FreeCAD; no routes/results are substituted.
"""
import json
import os
import subprocess
import time
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright

from native_first_candidate_browser import SEED
from task_state_browser import API, OUT, PRIVATE, login, read, card, consistent, save
from task_state_controls import open_task_session


CANCEL_REQUEST = '''import asyncio,json,sys
from uuid import UUID
from app.domain.identity import user_principal
from app.services.durable_submission import submit_durable_workflow
from app.services.operation_resolution import resolve_rest_generate_submission
from app.workflows.temporal import cancel_mcad_workflow
from app.db import close_database
async def main():
    p=user_principal(sys.argv[1]);f=json.loads(sys.argv[2])
    base=UUID(f['base_revision_id'])
    r=resolve_rest_generate_submission(base_revision_id=base,output_formats=['step','stl'])
    task=await submit_durable_workflow(p,project_id=UUID(f['project_id']),branch_id=UUID(f['document_id']),
        expected_base_revision_id=base,expected_state_version=0,idempotency_key='cancel-before-head-'+f['session_id'],
        operation='generate',objective='Create a plate 60 by 40 by 8 mm with one centered 6 mm through hole',
        output_formats=['step','stl'],modeling_backend=r.modeling_backend,operation_context=r.operation_context)
    await cancel_mcad_workflow(tenant_id=p.tenant_id,principal_id=p.principal_id,workflow_run_id=task.workflow_run_id)
    print('CANCEL_REQUEST='+str(task.workflow_run_id),flush=True)
    await close_database()
asyncio.run(main())
'''


def container_run(script, args, label):
    container = os.environ['CAD_NATIVE_E2E_API']
    assert container.startswith('cad-coedit-') and container.endswith('-api')
    result = subprocess.run([os.getenv('CAD_NATIVE_E2E_PODMAN', '/opt/homebrew/bin/podman'),
        'exec', '-i', container, 'python', '-', *args], input=script, text=True,
        capture_output=True, timeout=240)
    (OUT / (label + '.log')).write_text(result.stdout + result.stderr)
    assert result.returncode == 0, label + ' failed; inspect private log'
    return result.stdout


def seed(label):
    session, panel = str(uuid4()), str(uuid4())
    title = label + ' ' + session[:8]
    output = container_run(SEED, [PRIVATE['owner']['user']['id'], session, panel, title], label)
    fixture = json.loads(next(line.split('=', 1)[1] for line in output.splitlines() if line.startswith('FIRST_CANDIDATE=')))
    fixture.update(session_id=session, panel_id=panel, title=title)
    (OUT / (label + '.json')).write_text(json.dumps(fixture, indent=2))
    return fixture


def main():
    OUT.mkdir(parents=True, exist_ok=False); OUT.chmod(0o700)
    errors = []
    with httpx.Client(base_url=API, timeout=60, headers={'Authorization': 'Bearer ' + PRIVATE['owner']['token']}) as client, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            login(page)
            # Agent composer currently accepts more than the API's 4,000-character
            # objective bound. Submit real invalid input to exercise its rejection.
            open_task_session(page, client, os.environ['CAD_PHONE_WORKFLOW'])
            long_request = '仅生成概念手机壳，不验证适配。' * 300
            page.locator('textarea[aria-label="询问 Agent"]:visible').fill(long_request)
            page.get_by_role('button', name='审查请求', exact=True).click()
            intake = page.get_by_role('region', name='执行前需求确认', exact=True)
            intake.get_by_role('checkbox', name='确认仅生成概念外形', exact=True).check()
            intake.get_by_role('button', name='按概念外形继续', exact=True).click()
            consistent(page, 'failed')
            expect(card(page)).to_have_attribute('data-task-id', '', timeout=30000)
            expect(card(page)).to_contain_text('原需求已保留')
            card(page).get_by_role('button', name='修改保留的需求', exact=True).click()
            expect(page.locator('textarea[aria-label="询问 Agent"]:visible')).to_have_value(long_request)
            save(page, 'submission-rejected-with-request-preserved')

            # Reject a real first candidate, keeping its empty Head empty.
            rejected = seed('首次候选拒绝验收')
            page.reload(); page.get_by_role('button', name=rejected['title'], exact=False).first.click()
            consistent(page, 'candidate')
            page.get_by_role('button', name='审阅候选与参数变化', exact=True).click()
            dialog = page.get_by_role('dialog', name='变更审查', exact=True)
            dialog.get_by_role('textbox', name='审查意见', exact=True).fill('本候选不接受；核验拒绝后空文档未保存。')
            dialog.get_by_role('button', name='拒绝变更', exact=True).click()
            expect(dialog.get_by_text('审查状态：rejected', exact=True)).to_be_visible(timeout=30000)
            dialog.get_by_role('button', name='关闭', exact=True).last.click()
            consistent(page, 'needs_input')
            head = read(client, '/api/documents/' + rejected['document_id'])
            assert head['state_version'] == 0 and not head['fcstd']
            expect(page.get_by_test_id('document-scene')).to_have_count(0)
            page.reload(); consistent(page, 'needs_input')
            expect(page.get_by_test_id('document-scene')).to_have_count(0)
            save(page, 'rejected-candidate-not-saved-after-reload')

            # Preserve the exact draft and DOM editor while crossing responsive
            # boundaries and opening/closing the mobile inspector.
            page.reload(); open_task_session(page, client, os.environ['CAD_SAVED_WORKFLOW'])
            tree = page.get_by_test_id('cloud-document-panel')
            page.get_by_role('tab', name='特征与属性', exact=True).click()
            tree.get_by_role('treeitem', name='Hole', exact=True).click()
            field = tree.get_by_role('spinbutton', name='Hole.Diameter', exact=True)
            original = field.input_value(); field.fill(str(float(original) + 0.1))
            page.evaluate('window.__originalEditor = document.querySelector("[data-feature-properties]"); window.__originalCanvas = document.querySelector("[data-testid=document-scene] canvas")')
            for width in [1000, 390]:
                page.set_viewport_size({'width': width, 'height': 900})
                if width == 390:
                    page.locator('.workspace-header').get_by_role('button', name='预览', exact=True).click()
                page.get_by_role('button', name='参数', exact=True).click()
                overlay = page.get_by_role('dialog', name='机械设计检查器', exact=True)
                expect(overlay).to_be_visible()
                expect(field).to_have_value(str(float(original) + 0.1))
                assert page.evaluate('window.__originalEditor === document.querySelector("[data-feature-properties]")')
                save(page, 'preserved-draft-' + str(width))
                overlay.get_by_role('button', name='关闭机械设计检查器', exact=True).click()
                expect(field).to_be_hidden()
            page.set_viewport_size({'width': 1440, 'height': 1000})
            expect(field).to_have_value(str(float(original) + 0.1))
            assert page.evaluate('window.__originalCanvas === document.querySelector("[data-testid=document-scene] canvas")')
            page.reload()  # Explicitly discard this test-only unsent browser draft.

            # A real cancelled request freezes base 0. Committing another actual
            # candidate advances Head; retry must reject the old base atomically.
            stale = seed('重试基线冲突验收')
            output = container_run(CANCEL_REQUEST, [PRIVATE['owner']['user']['id'], json.dumps(stale)], 'cancel-stale-base')
            cancelled = next(line.split('=', 1)[1] for line in output.splitlines() if line.startswith('CANCEL_REQUEST='))
            until = time.monotonic() + 180
            while time.monotonic() < until:
                snapshot = read(client, '/api/tasks/' + cancelled + '/snapshot')
                if snapshot['status'] == 'cancelled': break
                page.wait_for_timeout(500)
            assert snapshot['status'] == 'cancelled', snapshot['status']
            client.post('/api/change-sets/' + stale['change_set_id'] + '/accept', json={'note': '真实基线冲突测试：提交另一个已检查候选'}).raise_for_status()
            client.post('/api/change-sets/' + stale['change_set_id'] + '/commit').raise_for_status()
            before = read(client, '/api/documents/' + stale['document_id'])
            retry = client.post('/api/tasks/' + cancelled + '/retry', json={'idempotency_key': str(uuid4())})
            assert retry.status_code == 409, retry.text()
            after = read(client, '/api/documents/' + stale['document_id'])
            assert before['head_revision_id'] == after['head_revision_id'] and after['state_version'] == 1
            assert not errors, errors
            report = {'status': 'passed', 'real_submission_rejection_preserves_request': True,
                'rejected_candidate_workflow': rejected['workflow_id'], 'rejected_first_candidate_empty_after_reload': True,
                'draft_editor_canvas_survive_responsive_layout': True, 'responsive_widths': [1440, 1000, 390],
                'cancelled_workflow': cancelled, 'stale_retry_status': retry.status_code, 'stale_retry_head_unchanged': True, 'page_errors': errors}
            (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print('CAD_TASK_BOUNDARIES=' + json.dumps(report, ensure_ascii=False))
        except BaseException:
            save(page, 'failure'); raise
        finally:
            context.tracing.stop(path=str(OUT / 'trace.zip')); browser.close()


if __name__ == '__main__':
    main()
