"""Mandatory monitor permissions and account filtering without invented usage."""
import json
import os
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright, expect

private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
out = Path(os.environ['CAD_MONITOR_E2E_REPORT']); out.mkdir(parents=True,exist_ok=False)
url = os.environ['CAD_MONITOR_E2E_URL']
with httpx.Client(base_url=url) as api:
    for endpoint in ('accounts','tasks','calls'):
        assert api.get('/api/monitor/'+endpoint).status_code == 401
        assert api.get('/api/monitor/'+endpoint,headers={'Authorization':'Bearer '+private['owner']['token']}).status_code == 403
with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True)
    page=browser.new_page(); errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    try:
        page.goto(url)
        for role in ('owner','admin'):
            page.get_by_label('账号',exact=True).fill(private[role]['phone'])
            page.get_by_label('密码',exact=True).fill(private[role]['password'])
            page.get_by_role('button',name='登录监控面板').click()
            if role == 'owner':
                expect(page.get_by_role('alert')).to_contain_text('仅平台管理员')
                expect(page.locator('#dashboard')).to_be_hidden()
        expect(page.get_by_role('heading',name='各账号使用情况')).to_be_visible()
        for role in ('owner','editor'):
            page.get_by_role('button',name=private[role]['phone'],exact=True).click()
            expect(page.locator('#details-title')).to_contain_text(private[role]['phone'])
            with page.expect_response(lambda response:'/api/monitor/calls?' in response.url) as calls_response:
                page.get_by_role('button',name='模型调用',exact=True).click()
            expect(page.locator('#detail-head')).to_contain_text('总 Token')
            # Use the actual filtered response. A configured stack may already
            # contain real visual checks from the native parameter regression.
            calls=calls_response.value.json()['items']
            assert all(item['account']=='user:'+private[role]['user']['id'] for item in calls)
            if calls:
                expect(page.locator('#details > tr')).to_have_count(len(calls))
                for item in calls:
                    expect(page.locator('#details')).to_contain_text(item['id'])
            else:
                expect(page.locator('#details')).to_contain_text('此范围内没有记录')
        page.reload()
        expect(page.get_by_role('heading',name='各账号使用情况')).to_be_visible()
        page.get_by_role('button',name='退出登录').click()
        page.reload()
        expect(page.get_by_role('heading',name='管理员登录')).to_be_visible()
        assert not errors,errors
        (out/'report.json').write_text(json.dumps({'passed':True,'non_admin_denied':True,
            'two_accounts':True,'persistent_reload':True,'logout':True,'page_errors':errors}))
    finally:
        browser.close()
