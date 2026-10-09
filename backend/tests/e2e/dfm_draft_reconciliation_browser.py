"""Real rule writes, lost replies and later drafts; never replace API responses.

The same scenarios run against Vite and the shipping frontend/NGINX image.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from transport_fault_proxy import TransportFaultProxy


def main():
    root = Path(__file__).resolve().parents[3]
    out = Path(os.environ['CAD_DRAFT_RECONCILIATION_REPORT'])
    out.mkdir(parents=True, exist_ok=False)
    people = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    api_url = os.environ['CAD_NATIVE_E2E_URL']
    packaged = os.environ.get('CAD_DRAFT_PACKAGED') == '1'
    cases = []
    with TransportFaultProxy(api_url) as proxy, (out / 'web.log').open('w') as log:
        server = None
        web = proxy.url
        try:
            if not packaged:
                with socket.socket() as reservation:
                    reservation.bind(('127.0.0.1', 0))
                    port = reservation.getsockname()[1]
                web = f'http://127.0.0.1:{port}'
                server = subprocess.Popen(['node', 'node_modules/vite/bin/vite.js', '--host', '127.0.0.1',
                    '--strictPort', '--port', str(port)], cwd=root / 'frontend',
                    env={**os.environ, 'CAD_API_TARGET': proxy.url}, stdout=log, stderr=subprocess.STDOUT)
                for _ in range(60):
                    try:
                        if httpx.get(web, trust_env=False).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert server.poll() is None
                    time.sleep(.2)
                else:
                    raise AssertionError('Draft test frontend did not start')
            with httpx.Client(base_url=api_url, trust_env=False, timeout=30,
                    headers={'Authorization': 'Bearer ' + people['owner']['token']}) as api, sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
                context = browser.new_context(viewport={'width':1440, 'height':1000})
                context.tracing.start(screenshots=True, snapshots=True)
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                def settings():
                    page.locator('.workspace-header').get_by_role('button', name='设置', exact=True).click()
                    drawer = page.get_by_role('dialog', name='设置', exact=True)
                    drawer.get_by_role('tab', name='DFM 规则', exact=True).click()
                    drawer.get_by_role('button', name='CNC 加工', exact=True).click()
                    return drawer
                def rules():
                    # The process endpoint intentionally returns enabled rules.
                    # Settings/readback use the complete sets, including disabled.
                    sets = api.get('/api/dfm/rules').raise_for_status().json()
                    return [rule for group in sets if group['process'] == 'CNC' for rule in group['rules']]
                def actual():
                    return next(r for r in rules() if r['id'] == rule['id'])
                def writes():
                    return proxy.counts.get(('PUT', target), 0)
                try:
                    page.goto(web, wait_until='domcontentloaded')
                    page.locator('#account').fill(people['owner']['phone'])
                    page.locator('#password').fill(people['owner']['password'])
                    page.locator('button[type=submit]').click()
                    expect(page.locator('.workspace-header')).to_be_visible(timeout=30000)
                    drawer = settings()
                    rule = next(r for r in rules() if r['id'] == 'cnc_max_size')
                    initial = rule['threshold_max']
                    target = '/api/dfm/rules/' + rule['id']
                    row = drawer.locator('.ww-rule').filter(has_text=rule['description'])
                    maximum = row.get_by_role('spinbutton', name='最大值 (mm)', exact=True)
                    save = row.get_by_role('button', name='保存阈值', exact=True)
                    retry = row.get_by_role('button', name='核对结果', exact=True)

                    maximum.fill(str(initial + 1))
                    proxy.drop_response_once = ('PUT', target)
                    proxy.block_reads = '/api/dfm/rules'
                    save.click()
                    expect(retry).to_be_enabled()
                    expect(save).to_be_disabled()
                    expect(maximum).to_be_enabled()
                    later = str(initial + 2)
                    maximum.fill(later)
                    assert actual()['threshold_max'] == initial + 1
                    assert writes() == 1
                    page.screenshot(path=str(out / 'before-reconciliation.png'))
                    proxy.block_reads = None
                    retry.click()
                    expect(retry).to_have_count(0)
                    observed = maximum.input_value()
                    (out / 'reconciliation.json').write_text(json.dumps({'submitted':initial + 1,
                        'server_value':actual()['threshold_max'], 'later_draft':later,
                        'after_readback':observed, 'put_count':writes()}, indent=2))
                    expect(maximum).to_have_value(later)
                    expect(save).to_be_enabled()
                    assert writes() == 1
                    cases.append('lost_committed_reply_preserves_subsequent_draft')

                    # Updating the baseline must not unregister the newer draft.
                    for leave in [drawer.get_by_role('tab', name='工艺知识图谱', exact=True),
                                  drawer.get_by_role('button', name='关闭设置', exact=True)]:
                        leave.click()
                        guard = page.get_by_role('dialog', name='保留编辑草稿', exact=True)
                        expect(guard).to_be_visible()
                        guard.get_by_role('button', name='继续编辑', exact=True).click()
                        expect(maximum).to_have_value(later)
                    original_page = page.evaluate('performance.timeOrigin')
                    # A cancelled reload has no navigation completion to wait
                    # for. Observe its real beforeunload dialog instead.
                    with page.expect_event('dialog') as unload:
                        page.evaluate('() => { setTimeout(() => window.location.reload(), 0); }')
                    assert unload.value.type == 'beforeunload'
                    unload.value.dismiss()
                    expect(maximum).to_have_value(later)
                    assert page.evaluate('performance.timeOrigin') == original_page
                    cases.append('navigation_and_refresh_keep_dirty_draft')

                    save.click()
                    expect(save).to_be_disabled()
                    expect(retry).to_have_count(0)
                    expect(maximum).to_be_enabled()
                    assert actual()['threshold_max'] == initial + 2
                    assert writes() == 2
                    # A confirmed saved draft is clean; reload uses actual server state.
                    page.reload(wait_until='domcontentloaded')
                    expect(page.locator('.workspace-header')).to_be_visible()
                    drawer = settings()
                    expect(maximum).to_have_value(str(initial + 2).removesuffix('.0'))
                    expect(save).to_be_disabled()
                    cases.append('explicit_save_then_clean_refresh')

                    # The same reconciliation must retain new input when PUT never arrived.
                    maximum.fill(str(initial + 3))
                    proxy.drop_request_once = ('PUT', target)
                    proxy.block_reads = '/api/dfm/rules'
                    save.click()
                    expect(retry).to_be_enabled()
                    maximum.fill(str(initial + 4))
                    assert actual()['threshold_max'] == initial + 2
                    proxy.block_reads = None
                    retry.click()
                    expect(retry).to_have_count(0)
                    expect(row.get_by_role('alert')).to_contain_text('草稿已保留')
                    expect(maximum).to_have_value(str(initial + 4))
                    save.click()
                    expect(maximum).to_be_enabled()
                    expect(save).to_be_disabled()
                    assert actual()['threshold_max'] == initial + 4 and writes() == 4
                    cases.append('undelivered_write_preserves_draft_and_explicit_retry')

                    # Empty later input must not be replaced or auto-submitted as zero.
                    maximum.fill(str(initial + 5))
                    proxy.drop_response_once = ('PUT', target)
                    proxy.block_reads = '/api/dfm/rules'
                    save.click()
                    expect(retry).to_be_enabled()
                    maximum.fill('')
                    proxy.block_reads = None
                    retry.click()
                    expect(retry).to_have_count(0)
                    expect(maximum).to_have_value('')
                    save.click()
                    expect(row.get_by_role('alert')).to_contain_text('有效阈值')
                    assert writes() == 5 and actual()['threshold_max'] == initial + 5
                    drawer.get_by_role('button', name='FDM 打印', exact=True).click()
                    page.get_by_role('dialog', name='保留编辑草稿', exact=True).get_by_role('button', name='放弃草稿并切换', exact=True).click()
                    drawer.get_by_role('button', name='CNC 加工', exact=True).click()
                    expect(maximum).to_have_value(str(initial + 5).removesuffix('.0'))
                    expect(save).to_be_disabled()
                    cases.append('empty_draft_validation_and_explicit_discard_use_latest_baseline')

                    # An enabled-only patch must not discard an unrelated threshold draft,
                    # even when the authoritative threshold changed in another client.
                    maximum.fill(str(initial + 6))
                    api.put(target, json={'threshold_max':initial + 7}).raise_for_status()
                    row.get_by_role('checkbox').click()
                    expect(row.get_by_role('checkbox')).to_be_enabled()
                    expect(maximum).to_have_value(str(initial + 6))
                    assert actual()['threshold_max'] == initial + 7 and actual()['enabled'] != rule['enabled']
                    save.click()
                    expect(maximum).to_be_enabled()
                    expect(save).to_be_disabled()
                    assert actual()['threshold_max'] == initial + 6
                    cases.append('enabled_only_readback_updates_baseline_without_replacing_draft')

                    minimum_rule = next(r for r in rules() if r['threshold_min'] is not None)
                    minrow = drawer.locator('.ww-rule').filter(has_text=minimum_rule['description'])
                    minimum = minrow.get_by_role('spinbutton', name=f"最小值 ({minimum_rule['unit']})", exact=True)
                    minimum.fill(str(minimum_rule['threshold_min']))
                    # Exercise normalization of a submitted minimum independently of maximum.
                    minsave = minrow.get_by_role('button', name='保存阈值', exact=True)
                    value = minimum_rule['threshold_min'] + .125
                    minimum.fill(str(value))
                    minsave.click()
                    expect(minimum).to_be_enabled()
                    expect(minsave).to_be_disabled()
                    assert next(r for r in rules() if r['id'] == minimum_rule['id'])['threshold_min'] == value
                    cases.append('minimum_threshold_normal_save_is_clean')

                    drawer.get_by_role('button', name='关闭设置', exact=True).click()
                    page.get_by_role('button', name='打开账号管理', exact=True).click()
                    page.get_by_role('button', name='退出登录', exact=True).click()
                    page.locator('#account').fill(people['admin']['phone'])
                    page.locator('#password').fill(people['admin']['password'])
                    page.locator('button[type=submit]').click()
                    expect(page.locator('.workspace-header')).to_be_visible()
                    page.get_by_role('button', name='打开账号管理', exact=True).click()
                    account = page.get_by_role('dialog', name='账号管理', exact=True)
                    code = 'CI' + uuid4().hex[:14].upper()
                    code_input = account.get_by_role('textbox', name='邀请码内容', exact=True)
                    uses = account.get_by_role('spinbutton', name='邀请码最大使用次数', exact=True)
                    code_input.fill(code)
                    uses.fill('2')
                    invite_path = '/api/auth/invites/' + code
                    proxy.drop_response_once = ('POST', '/api/auth/invites')
                    proxy.block_reads = invite_path
                    account.get_by_role('button', name='创建', exact=True).click()
                    invite_retry = account.get_by_role('button', name='核对结果', exact=True)
                    expect(invite_retry).to_be_enabled()
                    # This form clears its submitted values after confirmation;
                    # it must not accept a newer input while that result is unknown.
                    expect(code_input).to_be_disabled()
                    expect(uses).to_be_disabled()
                    expect(code_input).to_have_value(code)
                    expect(uses).to_have_value('2')
                    proxy.block_reads = None
                    invite_retry.click()
                    expect(invite_retry).to_have_count(0)
                    expect(code_input).to_be_enabled()
                    expect(uses).to_be_enabled()
                    expect(code_input).to_have_value('')
                    expect(uses).to_have_value('1')
                    invite = account.locator('.font-mono').filter(has_text=code).locator('..')
                    expect(invite.get_by_role('button', name='禁用', exact=True)).to_be_enabled()
                    assert proxy.counts[('POST', '/api/auth/invites')] == 1
                    cases.append('invite_inputs_locked_until_committed_result_reconciles')
                    assert not errors, errors
                    assert len(proxy.dropped_after_response) == 3
                    assert all(status == 200 for _, status in proxy.dropped_after_response)
                    page.screenshot(path=str(out / 'after-verification.png'))
                    (out / 'report.json').write_text(json.dumps({'passed':True,
                        'classification':'REAL_BROWSER_API_AND_TRANSPORT_FAILURE', 'packaged_frontend':packaged,
                        'cases':cases, 'lost_successful_write_responses':len(proxy.dropped_after_response),
                        'page_errors':errors}, ensure_ascii=False, indent=2))
                except BaseException:
                    page.screenshot(path=str(out / 'failure.png'))
                    (out / 'failure.txt').write_text(page.locator('body').inner_text())
                    raise
                finally:
                    proxy.block_reads = None
                    context.tracing.stop(path=str(out / 'trace.zip'))
                    browser.close()
        finally:
            if server is not None:
                server.terminate()
                server.wait(timeout=15)


if __name__ == '__main__':
    main()
