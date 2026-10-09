"""Real browser/API acceptance, including committed writes with lost responses."""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from transport_fault_proxy import TransportFaultProxy
from acceptance_release_contract import verify_release
from acceptance_restore_contract import verify_restore
from requirement_revision_browser import verify_requirement_intake


def main():
    root = Path(__file__).resolve().parents[3]
    out = Path(os.environ['CAD_ACCEPTANCE_FOLLOWUP_REPORT'])
    fixture = json.loads((out/'fixture.json').read_text())
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    api_url = os.environ['CAD_NATIVE_E2E_URL']
    records, metrics = [], []
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
    web = f'http://127.0.0.1:{port}'
    with TransportFaultProxy(api_url) as proxy, (out/'fault-web.log').open('w') as log:
        env = {**os.environ, 'CAD_API_TARGET': proxy.url}
        server = subprocess.Popen(['node', 'node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--strictPort', '--port', str(port)], cwd=root/'frontend', env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(60):
                try:
                    if httpx.get(web, trust_env=False).status_code == 200: break
                except httpx.HTTPError: pass
                assert server.poll() is None, 'Vite exited; see fault-web.log'
                time.sleep(.2)
            else: raise AssertionError('Followup browser server did not start')
            with httpx.Client(base_url=api_url, timeout=60, trust_env=False, headers={'Authorization': 'Bearer '+private['owner']['token']}) as api, sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
                context = browser.new_context(viewport={'width':1440,'height':1000}, has_touch=True)
                context.tracing.start(screenshots=True, snapshots=True)
                page = context.new_page(); errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                def login(account):
                    page.goto(web, wait_until='domcontentloaded')
                    page.locator('#account').fill(account['phone']); page.locator('#password').fill(account['password'])
                    page.locator('button[type=submit]').click()
                    expect(page.locator('.workspace-header')).to_be_visible(timeout=30000)
                def settings():
                    page.locator('.workspace-header').get_by_role('button', name='设置', exact=True).click()
                    return page.get_by_role('dialog', name='设置', exact=True)
                def call(method, path, **kwargs):
                    response = api.request(method, path, **kwargs); response.raise_for_status(); return response.json()
                try:
                    login(private['owner'])
                    page.get_by_role('button', name=fixture['title'], exact=False).first.click()
                    scene = page.get_by_test_id('document-scene')
                    expect(scene).to_have_attribute('data-revision', fixture['document']['head_revision_id'], timeout=90000)
                    page.get_by_role('button', name='属性', exact=True).click()
                    for width in [1440, 768, 390, 320]:
                        page.set_viewport_size({'width':width, 'height':900})
                        if width <= 390 and page.get_by_role('button', name='关闭工程信息', exact=True).is_visible():
                            page.get_by_role('button', name='关闭工程信息', exact=True).click()
                        if width <= 390 and page.locator('.workspace-header').get_by_role('button', name='预览', exact=True).is_visible():
                            page.locator('.workspace-header').get_by_role('button', name='预览', exact=True).click()
                        source = page.locator('.ww-version-source > summary'); source.click()
                        popup = page.locator('.ww-version-source > div')
                        expect(popup).to_be_visible()
                        bounds = popup.evaluate('''e=>{const r=e.getBoundingClientRect();let left=0,right=innerWidth;
                            for(let p=e.parentElement;p;p=p.parentElement){if(/hidden|clip|auto|scroll/.test(getComputedStyle(p).overflowX)){const b=p.getBoundingClientRect();left=Math.max(left,b.left);right=Math.min(right,b.right)}}
                            return {left:r.left,right:r.right,clipLeft:left,clipRight:right,scrollWidth:e.scrollWidth,clientWidth:e.clientWidth}}''')
                        assert bounds['left'] >= bounds['clipLeft'] - 1 and bounds['right'] <= bounds['clipRight'] + 1, (width, bounds)
                        assert bounds['scrollWidth'] <= bounds['clientWidth'] + 1
                        if width <= 390:
                            for selector in ('.ww-version-source > summary', '.ww-model-tree-shelf > summary', '.ww-view-actions > summary'):
                                entry = page.locator(selector)
                                assert entry.bounding_box()['height'] >= 44, selector
                                assert 'svg' in entry.evaluate("e=>getComputedStyle(e,'::before').maskImage"), selector
                        metrics.append({'width':width, 'source':bounds})
                        page.screenshot(path=str(out/f'source-{width}.png')); source.click()
                    tree = page.locator('.ww-model-tree-shelf'); tree.locator('summary').click()
                    tree.get_by_role('treeitem', name=fixture['long_name'], exact=True).click()
                    tree.locator('summary').click()
                    selected = page.get_by_test_id('viewport-selection')
                    expect(selected).to_be_visible()
                    selected.tap()
                    name_dialog = page.get_by_role('dialog', name='已选中特征', exact=True)
                    expect(name_dialog.locator('p')).to_contain_text(fixture['long_name'])
                    expect(name_dialog.locator('p')).to_be_visible()
                    name_dialog.get_by_role('button', name='关闭', exact=True).click()
                    selected.focus(); page.keyboard.press('Enter')
                    expect(name_dialog).to_be_visible(); name_dialog.get_by_role('button', name='关闭', exact=True).click()
                    records.append('source_bounds_full_uuid_mobile_targets_svg_and_long_name_touch_keyboard')
                    page.set_viewport_size({'width':1440, 'height':1000})
                    page.get_by_role('button', name='属性', exact=True).click()
                    left = page.locator('.ww-inspector-pane .ww-model-tree h3').bounding_box()['x']
                    page.get_by_role('button', name='版本', exact=True).click()
                    version_left = page.locator('.ww-inspector-pane').get_by_text('零件与历史版本', exact=True).bounding_box()['x']
                    assert abs(left - version_left) <= 1, (left, version_left)
                    context.set_offline(True)
                    expect(page.locator('.workspace-status')).to_contain_text('已断开', timeout=30000)
                    expect(page.locator('.workspace-status')).to_have_attribute('data-status', 'offline')
                    context.set_offline(False)
                    expect(page.locator('.workspace-status')).to_contain_text('文档已同步', timeout=30000)
                    records.append('panel_alignment_and_real_disconnect_reconnect')
                    drawer = settings()
                    drawer.get_by_role('tab', name='工艺知识图谱', exact=True).click()
                    drawer.get_by_role('button', name='智能推荐', exact=True).click()
                    dimension = drawer.get_by_role('spinbutton', name='最大尺寸 (mm)', exact=True)
                    dimension.fill('123')
                    drawer.get_by_role('textbox', name='材料偏好', exact=True).fill('Aluminum')
                    drawer.get_by_role('tab', name='DFM 规则', exact=True).click()
                    expect(dimension).to_be_hidden()
                    drawer.get_by_role('tab', name='工艺知识图谱', exact=True).click()
                    expect(dimension).to_have_value('123')
                    drawer.get_by_role('button', name='关闭设置', exact=True).click()
                    drawer = settings(); drawer.get_by_role('button', name='智能推荐', exact=True).click()
                    expect(drawer.get_by_role('spinbutton', name='最大尺寸 (mm)', exact=True)).to_have_value('123')
                    expect(drawer.get_by_role('textbox', name='材料偏好', exact=True)).to_have_value('Aluminum')
                    records.append('recommendation_draft_survives_tabs_and_close')
                    drawer.get_by_role('button', name='供应商', exact=True).click()
                    drawer.get_by_role('button', name='+ 添加供应商', exact=True).click()
                    create = page.get_by_role('dialog', name='添加供应商', exact=True)
                    supplier_name = 'Receipt supplier ' + uuid4().hex[:8]
                    create.get_by_role('textbox', name='供应商名称', exact=True).fill(supplier_name)
                    proxy.drop_response_once = ('POST', '/api/knowledge/nodes')
                    proxy.block_reads = '/api/knowledge/nodes/'
                    create.get_by_role('button', name='保存', exact=True).click()
                    expect(create.get_by_role('button', name='核对结果', exact=True)).to_be_enabled()
                    expect(create.get_by_role('button', name='保存', exact=True)).to_be_disabled()
                    supplier = next(item for item in call('GET', '/api/knowledge/nodes?type=supplier') if item['name'] == supplier_name)
                    create.get_by_role('button', name='取消', exact=True).click()
                    drawer.get_by_role('button', name='关闭设置', exact=True).click()
                    drawer = settings()
                    expect(drawer.get_by_role('button', name='核对结果', exact=True)).to_be_enabled()
                    proxy.block_reads = None
                    drawer.get_by_role('button', name='核对结果', exact=True).click()
                    expect(drawer.get_by_role('button', name='核对结果', exact=True)).to_have_count(0)
                    drawer.get_by_role('button', name='供应商', exact=True).click()
                    row = drawer.locator('article').filter(has_text=supplier_name)
                    expect(row).to_be_visible()
                    target = '/api/knowledge/nodes/' + supplier['id']
                    proxy.drop_response_once = ('DELETE', target); proxy.block_reads = target
                    row.get_by_role('button', name='删除', exact=True).click()
                    expect(drawer.get_by_role('button', name='核对结果', exact=True)).to_be_enabled()
                    assert api.get(target + '?customer_id=default').status_code == 404
                    expect(row.get_by_role('button', name='删除', exact=True)).to_be_disabled()
                    proxy.block_reads = None
                    drawer.get_by_role('button', name='核对结果', exact=True).click()
                    expect(row).to_have_count(0)
                    assert proxy.counts[('DELETE', target)] == 1
                    # A cut before delivery must preserve the actual row and allow
                    # retry only after readback proves the desired deletion absent.
                    call('POST', '/api/knowledge/nodes', json=supplier)
                    drawer.get_by_role('button', name='智能推荐', exact=True).click()
                    drawer.get_by_role('button', name='供应商', exact=True).click()
                    expect(row).to_be_visible(); proxy.drop_request_once = ('DELETE', target)
                    row.get_by_role('button', name='删除', exact=True).click()
                    expect(drawer.get_by_role('alert')).to_contain_text('已核对服务器')
                    expect(row.get_by_role('button', name='删除', exact=True)).to_be_enabled()
                    assert api.get(target + '?customer_id=default').status_code == 200
                    call('DELETE', target + '?customer_id=default')
                    records.append('supplier_create_delete_real_commit_lost_reply_readback_and_not_delivered')
                    drawer.get_by_role('tab', name='DFM 规则', exact=True).click()
                    drawer.get_by_role('button', name='CNC 加工', exact=True).click()
                    rule = next(item for item in call('GET','/api/dfm/rules/CNC') if item['id']=='cnc_max_size')
                    rule_row = drawer.locator('.ww-rule').filter(has_text=rule['description'])
                    maximum = rule_row.get_by_role('spinbutton', name='最大值 (mm)', exact=True)
                    maximum.fill(str(rule['threshold_max'] + 1))
                    proxy.drop_response_once = ('PUT', '/api/dfm/rules/cnc_max_size'); proxy.block_reads = '/api/dfm/rules'
                    rule_row.get_by_role('button', name='保存阈值', exact=True).click()
                    expect(rule_row.get_by_role('button', name='核对结果', exact=True)).to_be_enabled()
                    expect(rule_row.get_by_role('button', name='保存阈值', exact=True)).to_be_disabled()
                    actual = next(item for item in call('GET','/api/dfm/rules/CNC') if item['id']=='cnc_max_size')
                    assert actual['threshold_max'] == rule['threshold_max'] + 1
                    proxy.block_reads = None; rule_row.get_by_role('button', name='核对结果', exact=True).click()
                    expect(rule_row.get_by_role('button', name='核对结果', exact=True)).to_have_count(0)
                    assert proxy.counts[('PUT', '/api/dfm/rules/cnc_max_size')] == 1
                    drawer.get_by_role('button', name='关闭设置', exact=True).click()
                    call('PUT', '/api/dfm/rules/cnc_max_size', json={'threshold_max':rule['threshold_max']})
                    records.append('dfm_save_real_commit_lost_reply_and_readback_without_repeat_write')
                    verify_release(page, api, private, fixture, out)
                    records.append('formal_release_selected_evidence_verified_download_and_role_matrix')
                    verify_restore(page, api, fixture, out)
                    records.append('history_restore_new_candidate_commit_reopen_and_old_evidence_rejection')
                    verify_requirement_intake(page, api, fixture, out)
                    records.append('revised_requirement_basis_durable_admission_and_cancel_preserves_history')
                    page.get_by_role('button', name='打开账号管理', exact=True).click()
                    page.get_by_role('button', name='退出登录', exact=True).click()
                    login(private['admin'])
                    page.get_by_role('button', name='打开账号管理', exact=True).click()
                    account = page.get_by_role('dialog', name='账号管理', exact=True)
                    code = 'CI' + uuid4().hex[:14].upper()
                    account.get_by_role('textbox', name='邀请码内容', exact=True).fill(code)
                    invite_path = '/api/auth/invites/' + code
                    proxy.drop_response_once = ('POST', '/api/auth/invites'); proxy.block_reads = invite_path
                    account.get_by_role('button', name='创建', exact=True).click()
                    expect(account.get_by_role('button', name='核对结果', exact=True)).to_be_enabled()
                    expect(account.get_by_role('button', name='创建', exact=True)).to_be_disabled()
                    proxy.block_reads = None; account.get_by_role('button', name='核对结果', exact=True).click()
                    invite = account.locator('.font-mono').filter(has_text=code).locator('..')
                    expect(invite.get_by_role('button', name='禁用', exact=True)).to_be_enabled()
                    proxy.drop_response_once = ('DELETE', invite_path); proxy.block_reads = invite_path
                    invite.get_by_role('button', name='禁用', exact=True).click()
                    expect(account.get_by_role('button', name='核对结果', exact=True)).to_be_enabled()
                    proxy.block_reads = None; account.get_by_role('button', name='核对结果', exact=True).click()
                    expect(invite.get_by_text('已禁用', exact=True)).to_be_visible()
                    assert proxy.counts[('POST', '/api/auth/invites')] == 1 and proxy.counts[('DELETE', invite_path)] == 1
                    assert api.get(invite_path).status_code == 403
                    assert httpx.get(api_url + invite_path, trust_env=False).status_code == 401
                    assert len(proxy.dropped_after_response) == 5 and all(200 <= status < 300 for _, status in proxy.dropped_after_response)
                    records.append('invite_create_disable_lost_reply_actual_admin_readback_and_role_denial')
                    page.screenshot(path=str(out/'verified-mutations.png'))
                    assert not errors, errors
                    (out/'browser.json').write_text(json.dumps({'passed':True, 'classification':'REAL_BROWSER_API_AND_TRANSPORT_FAILURE', 'cases':records, 'source_bounds':metrics, 'lost_successful_write_responses':len(proxy.dropped_after_response), 'page_errors':errors}, ensure_ascii=False, indent=2))
                    print('ACCEPTANCE FOLLOWUP BROWSER PASSED')
                except BaseException:
                    page.screenshot(path=str(out/'failure.png'))
                    (out/'failure.txt').write_text(page.locator('body').inner_text())
                    raise
                finally:
                    proxy.block_reads = None
                    context.tracing.stop(path=str(out/'trace.zip')); browser.close()
        finally:
            server.terminate(); server.wait(timeout=15)


if __name__ == '__main__': main()
