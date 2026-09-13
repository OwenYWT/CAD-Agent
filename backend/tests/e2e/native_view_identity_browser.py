"""Real historical/current view identity, delayed reads and downloaded bytes.

Only delivery of one genuine history response is delayed. No response body or
provider output is fabricated. The browser must retain the later chosen view.
"""
import hashlib
import json
import os
from pathlib import Path
import re

import httpx
from playwright.sync_api import expect, sync_playwright


def main():
    base = os.environ['CAD_NATIVE_E2E_URL']
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    out = Path(os.environ['CAD_COEDIT_REPORT_DIR'])
    out.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(base_url=base, timeout=60,
        headers={'Authorization':'Bearer ' + private['owner']['token']})
    path = '/api/documents/' + private['document_id']
    def read(url):
        response = client.get(url)
        response.raise_for_status()
        return response.json()
    head = read(path)
    history = read('/api/history/panels/' + private['panel_id'] + '/snapshots')
    native = []
    for item in history:
        if item['id'] == head['head_revision_id']:
            continue
        view = read(path + '/revisions/' + item['id'])
        if view['snapshot'].get('files', {}).get('fcstd') and view['review_status'] in {'committed', 'rolled_back'}:
            native.append((item, view))
    assert len(native) >= 2
    first, chosen = native[-1], native[0]
    held = {}
    errors = []
    report = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        context = browser.new_context(viewport={'width':1440,'height':1000}, accept_downloads=True)
        context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page()
        page.set_default_timeout(30000)
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(base)
            page.locator('input[type="text"]').fill(private['owner']['phone'])
            page.locator('input[type="password"]').fill(private['owner']['password'])
            page.locator('button[type="submit"]').click()
            page.get_by_role('button', name='Create a rectangular plate', exact=False).first.click()
            expect(page.get_by_test_id('view-identity')).to_have_attribute('data-revision', head['head_revision_id'], timeout=30000)
            expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision', head['head_revision_id'], timeout=90000)
            page.get_by_role('tab', name='版本', exact=True).click()
            def row(item):
                return page.locator('div.rounded-lg.border.p-3').filter(has_text='修订 #' + str(item['version']) + ' ·').last
            def delay_actual_history(route):
                held['route'] = route
                held['response'] = route.fetch()
                assert held['response'].status == 200
            # Resolve the actual history endpoint used by getModelSnapshot.
            pattern = re.compile(r'/api/history/snapshots/' + first[0]['id'] + r'(?:\?.*)?$')
            page.route(pattern, delay_actual_history)
            row(first[0]).get_by_role('button', name='查看此版本', exact=True).click()
            for _ in range(100):
                if held:
                    break
                page.wait_for_timeout(100)
            assert held, 'expected to hold the original server history response'
            row(chosen[0]).get_by_role('button', name='查看此版本', exact=True).click()
            expect(page.get_by_test_id('view-identity')).to_have_attribute('data-revision', chosen[0]['id'], timeout=30000)
            expect(page.get_by_test_id('view-identity')).to_have_attribute('data-mode', 'history')
            held['route'].fulfill(response=held['response'])
            page.unroute(pattern)
            page.wait_for_timeout(500)
            expect(page.get_by_test_id('view-identity')).to_have_attribute('data-revision', chosen[0]['id'])
            expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision', chosen[0]['id'], timeout=90000)
            page.get_by_role('tab', name='文档', exact=True).click()
            panel = page.get_by_test_id('cloud-document-panel')
            panel.get_by_role('treeitem', name='Pad', exact=True).click()
            expected_length = next(p['value'] for f in chosen[1]['features'] for p in f['parameters'] if p['id'] == 'Pad.Length')
            field = panel.get_by_role('spinbutton', name='Pad.Length', exact=True)
            expect(field).to_have_value(str(expected_length).removesuffix('.0'))
            expect(field).to_be_disabled()
            composer = page.locator('textarea[aria-label="询问 Agent"]:visible').first
            composer.fill('把当前查看的历史模型改厚 1 mm')
            expect(page.get_by_role('button', name='审查请求', exact=True)).to_be_disabled()
            composer.fill('')
            def download_revision(view, filename):
                page.get_by_role('button', name='导出', exact=True).first.click()
                dialog = page.get_by_role('dialog', name='导出工程产物')
                expect(dialog).to_contain_text(view['revision_id'][:8])
                item = dialog.locator('div.flex.min-h-\\[66px\\]').filter(has_text='model.step')
                with page.expect_download() as downloaded:
                    item.get_by_role('button', name='下载', exact=True).click()
                destination = out / filename
                downloaded.value.save_as(destination)
                actual = client.get(view['snapshot']['files']['step'])
                actual.raise_for_status()
                assert destination.read_bytes() == actual.content
                dialog.get_by_role('button', name='关闭', exact=True).last.click()
                return hashlib.sha256(actual.content).hexdigest()
            historical_sha = download_revision(chosen[1], 'historical.step')
            page.screenshot(path=str(out / 'history-readonly.png'))
            button = page.get_by_role('button', name='查看已提交版本', exact=True)
            button.focus()
            button.press('Enter')
            expect(page.get_by_test_id('view-identity')).to_have_attribute('data-revision', head['head_revision_id'], timeout=30000)
            expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision', head['head_revision_id'], timeout=90000)
            current_view = read(path + '/revisions/' + head['head_revision_id'])
            current_sha = download_revision(current_view, 'current.step')
            assert current_sha != historical_sha
            # A previous committed change cannot roll back the current head.
            old_change = chosen[1]['change_set_id']
            old_detail = read('/api/change-sets/' + old_change)
            assert not old_detail['can_rollback']
            denied = client.post('/api/change-sets/' + old_change + '/rollback', json={'note':'Must not roll back a non-current commit'})
            assert denied.status_code == 409, denied.text
            after = read(path)
            assert (after['head_revision_id'],after['state_version']) == (head['head_revision_id'],head['state_version'])
            report = {'status':'passed', 'held_revision':first[0]['id'], 'chosen_historical_revision':chosen[0]['id'],
                'current_revision':head['head_revision_id'], 'late_history_response_ignored':True,
                'historical_parameter_value':expected_length, 'history_edit_and_agent_disabled':True,
                'history_step_sha256':historical_sha, 'current_step_sha256':current_sha,
                'viewer_parameters_and_downloads_same_revision':True, 'noncurrent_rollback_status':denied.status_code,
                'head_unchanged':True, 'page_errors':errors}
            assert not errors, errors
            (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print('CAD_VIEW_IDENTITY=' + json.dumps(report, ensure_ascii=False), flush=True)
        except BaseException:
            page.screenshot(path=str(out / 'failure.png'))
            (out / 'failure.txt').write_text(page.locator('body').inner_text())
            raise
        finally:
            context.tracing.stop(path=str(out / 'trace.zip'))
            browser.close()
            client.close()


if __name__ == '__main__':
    main()
