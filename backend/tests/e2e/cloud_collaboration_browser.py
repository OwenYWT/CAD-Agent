"""Two actual browser accounts edit, review and revoke project membership."""
import json
import os
from pathlib import Path
import time

import httpx
from playwright.sync_api import expect, sync_playwright
from cloud_document_acceptance import accept_in_browser

PRIVATE = Path(os.environ['CAD_NATIVE_E2E_PRIVATE'])
BASE = os.environ['CAD_NATIVE_E2E_URL']
WEB = os.environ['CAD_NATIVE_E2E_WEB']
REPORT = Path('/tmp/cad-expansion-collaboration-browser.json')
SHOTS = Path('/tmp/cad-expansion-collaboration-browser')


def main():
    private = json.loads(PRIVATE.read_text())
    model = json.loads(Path('/tmp/cad-expansion-collaboration-document.json').read_text())
    doc = '/api/documents/' + model['document_id']
    client = httpx.Client(base_url=BASE, timeout=30, headers={'Authorization': 'Bearer ' + private['owner']['token']})
    invitation = client.post(doc + '/invitations?role=editor')
    invitation.raise_for_status()
    with httpx.Client(base_url=BASE, timeout=30, headers={'Authorization': 'Bearer ' + private['editor']['token']}) as invited:
        accepted = invited.post(doc + '/invitations/accept', json={'tenant_id': model['tenant_id'], 'token': invitation.json()['token']})
        accepted.raise_for_status()
    def get(path):
        response = client.get(path); response.raise_for_status(); return response.json()
    before = get(doc)
    target = next(p['value'] for f in before['features'] for p in f['parameters'] if p['id'] == 'PadB.Length') + 1
    members = get(doc + '/members')['members']
    editors = [m for m in members if m['role'] == 'editor']
    assert len(editors) == 1
    editor_id = editors[0]['principal_id']
    url = WEB + '?document=' + model['document_id'] + '&workspace=' + model['tenant_id']
    report = {}
    def record(stage, **facts):
        report[stage] = facts; REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2)); print(stage, facts, flush=True)
    def login(page, person):
        page.goto(WEB)
        page.locator('input[type="text"]').fill(person['phone'])
        page.locator('input[type="password"]').fill(person['password'])
        page.locator('button[type="submit"]').click()
        expect(page.get_by_role('button', name='新建设计', exact=True)).to_be_visible(timeout=20000)
        page.goto(url)
        panel = page.get_by_test_id('cloud-document-panel')
        expect(panel.get_by_role('treeitem', name='PadB', exact=True)).to_be_visible(timeout=20000)
        panel.get_by_role('treeitem', name='PadB', exact=True).click()
        return panel
    SHOTS.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        owner = browser.new_context(viewport={'width':1440, 'height':1000}).new_page()
        editor = browser.new_context(viewport={'width':1280, 'height':1000}).new_page()
        errors = []
        for page in [owner, editor]: page.on('pageerror', lambda e: errors.append(str(e)))
        owner_panel = login(owner, private['owner'])
        panel = login(editor, private['editor'])
        expect(editor.locator('canvas')).to_be_visible(timeout=20000)
        assert editor.locator('canvas').evaluate("c => !!c.getContext('webgl2')")
        panel.get_by_role('spinbutton', name='PadB.Length', exact=True).fill(str(target))
        expect(panel.get_by_text('已保留此特征的编辑租约。', exact=False)).to_be_visible(timeout=10000)
        with editor.expect_response(lambda r: r.url.endswith(doc + '/operations') and r.request.method == 'POST') as submitted:
            panel.get_by_role('button', name='提交参数变更', exact=True).click()
        assert submitted.value.status == 202, submitted.value.text()
        task_id = submitted.value.json()['workflow_run_id']
        editor.reload()
        expect(editor.get_by_role('region', name='文档任务')).to_be_visible(timeout=15000)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            task = get('/api/tasks/' + task_id + '/snapshot')
            if task['status'] == 'waiting_confirmation':
                button = editor.get_by_role('button', name='确认并继续', exact=True)
                expect(button).to_be_enabled(timeout=15000); button.click()
            elif task['status'] in ['succeeded', 'failed', 'cancelled', 'timed_out']: break
            editor.wait_for_timeout(2000)
        assert task['status'] == 'succeeded', task.get('error_message')
        expect(editor.get_by_role('button', name='审阅任务变更', exact=True)).to_be_visible(timeout=15000)
        editor.get_by_role('button', name='审阅任务变更', exact=True).click()
        dialog = editor.get_by_role('dialog')
        accept_in_browser(dialog, '已阅读检查结果与视觉测量限制；本次仅修改 PadB 参数，并核对提交后的原生参数。')
        expect(dialog.get_by_text('审查状态：accepted', exact=True)).to_be_visible(timeout=15000)
        expect(dialog.get_by_role('button', name='提交版本', exact=True)).to_be_disabled()
        editor.screenshot(path=str(SHOTS / 'editor-review.png'))
        dialog.get_by_role('button', name='关闭', exact=True).last.click()
        owner_panel.get_by_role('button', name='查看变更', exact=True).first.click()
        owner_dialog = owner.get_by_role('dialog')
        expect(owner_dialog.get_by_role('button', name='提交版本', exact=True)).to_be_enabled(timeout=15000)
        owner_dialog.get_by_role('button', name='提交版本', exact=True).click()
        expect(owner_dialog.get_by_text('审查状态：committed', exact=True)).to_be_visible(timeout=20000)
        owner_dialog.get_by_role('button', name='关闭', exact=True).last.click()
        panel = editor.get_by_test_id('cloud-document-panel')
        panel.get_by_role('treeitem', name='PadB', exact=True).click()
        expect(panel.get_by_role('spinbutton', name='PadB.Length', exact=True)).to_have_value(f'{target:g}', timeout=15000)
        record('standalone_edit', task_id=task_id, restored_pending_task=True, actual_kernel=True, editor_cannot_commit=True, owner_committed=True, final_length_mm=target)
        # A live lease must be cleared when membership is downgraded.
        panel.get_by_role('spinbutton', name='PadB.Length', exact=True).focus()
        expect(panel.get_by_text('已保留此特征的编辑租约。', exact=False)).to_be_visible(timeout=10000)
        owner_panel.get_by_role('button', name='管理项目成员', exact=True).click()
        row = owner_panel.get_by_test_id('member-' + editor_id)
        expect(row.get_by_role('combobox')).to_be_visible(timeout=10000)
        row.get_by_role('combobox').select_option('viewer')
        expect(panel.get_by_role('spinbutton', name='PadB.Length', exact=True)).to_have_count(0, timeout=20000)
        assert all(l['principal_id'] != editor_id for l in get(doc + '/collaboration')['leases'])
        row.get_by_role('button', name='移除成员', exact=True).click()
        expect(editor.get_by_role('status').filter(has_text='文档访问权限已失效')).to_be_visible(timeout=20000)
        editor.screenshot(path=str(SHOTS / 'revoked-access.png'))
        record('membership', downgraded_without_reload=True, lease_cleared=True, revoked_socket_and_ui=True, page_errors=errors)
        assert not errors, errors
        browser.close()
    client.close()


main()
