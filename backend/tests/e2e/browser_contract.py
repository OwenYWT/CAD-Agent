"""CI browser contract: real auth, native kernel, candidate and saved revision.

No Provider or intercepted HTTP is used here. Provider-driven modifications are
covered by task_state_browser and reported as a separate live acceptance gate.
"""
import json
import os
from pathlib import Path
from uuid import uuid4
import httpx
from playwright.sync_api import sync_playwright, expect
from native_first_candidate_browser import SEED
from runtime_fixture import run_native_seed

private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
out = Path(os.environ['CAD_BROWSER_CONTRACT_REPORT']); out.mkdir(parents=True, exist_ok=False)
session, panel = str(uuid4()), str(uuid4())
title = 'Browser contract ' + session[:8]
result = run_native_seed(SEED, [private['owner']['user']['id'], session, panel, title])
(out/'seed.log').write_text(result.stdout + result.stderr)
assert result.returncode == 0, 'native fixture failed'
fixture = json.loads(next(line.split('=', 1)[1] for line in result.stdout.splitlines() if line.startswith('FIRST_CANDIDATE=')))
errors = []
with httpx.Client(base_url=os.environ['CAD_NATIVE_E2E_URL'], headers={'Authorization': 'Bearer ' + private['owner']['token']}) as api:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page = browser.new_page(viewport={'width':1440,'height':1000})
        page.context.tracing.start(screenshots=True, snapshots=True)
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(os.environ['CAD_NATIVE_E2E_WEB'])
            page.locator('input[type=text]').fill(private['owner']['phone'])
            page.locator('input[type=password]').fill(private['owner']['password'])
            page.locator('button[type=submit]').click()
            page.get_by_role('button', name=title, exact=False).first.click()
            scene = page.get_by_test_id('document-scene')
            expect(scene).to_have_attribute('data-revision', __import__('re').compile('.+'), timeout=90000)
            page.evaluate('window.__contractCanvas=document.querySelector("[data-testid=document-scene] canvas")')
            page.get_by_role('button', name='审阅候选与参数变化', exact=True).click()
            dialog = page.get_by_role('dialog', name='变更审查')
            expect(dialog.get_by_role('button', name='应用修改', exact=True)).to_be_visible()
            dialog.get_by_role('button', name='应用修改', exact=True).click()
            expect(dialog.get_by_text('审查状态：committed', exact=True)).to_be_visible(timeout=30000)
            dialog.get_by_role('button', name='关闭', exact=True).last.click()
            for _ in range(100):
                response = api.get('/api/documents/' + fixture['document_id']); response.raise_for_status()
                saved = response.json()
                if saved['state_version'] == 1: break
                page.wait_for_timeout(100)
            assert saved['state_version'] == 1 and saved['head_revision_id'] != fixture['base_revision_id']
            expect(scene).to_have_attribute('data-revision', saved['head_revision_id'], timeout=90000)
            assert page.evaluate('window.__contractCanvas===document.querySelector("[data-testid=document-scene] canvas")')
            for name in ('属性', '检查', '版本', '导出'):
                page.get_by_role('button', name=name, exact=True).click()
                expect(page.locator('.ww-inspector-pane:visible')).to_have_count(1)
                assert page.evaluate('window.__contractCanvas===document.querySelector("[data-testid=document-scene] canvas")')
                page.get_by_role('button', name='返回 Agent', exact=True).click()
            page.reload()
            expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision', saved['head_revision_id'], timeout=90000)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            page.screenshot(path=str(out/'saved-mobile.png'))
            (out/'report.json').write_text(json.dumps({'passed':True,'fixture':fixture,'saved_revision':saved['head_revision_id'],'real_kernel':True,'real_auth':True,'same_canvas':True,'page_errors':errors},indent=2))
            print('BROWSER CONTRACT PASSED')
        except BaseException:
            (out/'failure.txt').write_text(page.locator('body').inner_text())
            page.screenshot(path=str(out/'failure.png'))
            raise
        finally:
            page.context.tracing.stop(path=str(out/'trace.zip'))
            browser.close()
