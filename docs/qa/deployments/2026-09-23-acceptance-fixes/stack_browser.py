import json
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
import httpx
R=Path('/private/tmp/cad-fixes-20260923');d=json.loads((R/'stack-private.json').read_text());report=json.loads((R/'full-stack-report.json').read_text())
assert report.get('passed') is True
revisions={s['result']['revision_id'] for s in report['steps']};errors=[];verified=[]
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
 p=browser.new_page(viewport={'width':1440,'height':1000});p.on('pageerror',lambda e:errors.append(str(e)))
 p.goto('http://127.0.0.1:18160');p.locator('input[type=text]').fill(d['owner']['phone']);p.locator('input[type=password]').fill(d['owner']['password']);p.locator('button[type=submit]').click()
 buttons=p.get_by_role('button',name=__import__('re').compile('^Create a rectangular plate'));expect(buttons).to_have_count(2,timeout=30000)
 for i in range(2):
  buttons.nth(i).click();scene=p.get_by_test_id('document-scene');expect(scene).to_have_attribute('data-revision',__import__('re').compile('.+'),timeout=90000)
  if verified:expect(scene).to_have_attribute('data-revision',next(iter(revisions-set(verified))),timeout=90000)
  revision=scene.get_attribute('data-revision');assert revision in revisions and revision not in verified,(revision,verified,revisions)
  expect(scene.locator('canvas')).to_be_visible();assert scene.locator('canvas').evaluate("c=>!!c.getContext('webgl2')")
  p.evaluate('window.__drillCanvas=document.querySelector("[data-testid=document-scene] canvas")')
  for name in ('属性','检查','版本','导出'):
   p.get_by_role('button',name=name,exact=True).click();expect(p.locator('.ww-inspector-pane:visible')).to_have_count(1)
   assert p.evaluate('window.__drillCanvas===document.querySelector("[data-testid=document-scene] canvas")')
   p.get_by_role('button',name='返回 Agent',exact=True).click()
  p.reload();expect(p.get_by_test_id('document-scene')).to_have_attribute('data-revision',revision,timeout=90000);verified.append(revision)
 p.screenshot(path=str(R/'full-stack-browser.png'))
 m=browser.new_page();m.on('pageerror',lambda e:errors.append(str(e)));m.goto('http://127.0.0.1:18061')
 for role in ('owner','admin'):
  m.get_by_label('账号',exact=True).fill(d[role]['phone']);m.get_by_label('密码',exact=True).fill(d[role]['password']);m.get_by_role('button',name='登录监控面板').click()
  if role=='owner':expect(m.get_by_role('alert')).to_contain_text('仅平台管理员')
 expect(m.get_by_role('heading',name='各账号使用情况')).to_be_visible();m.get_by_role('button',name=d['owner']['phone'],exact=True).click();m.get_by_role('button',name='模型调用',exact=True).click();expect(m.locator('#detail-head')).to_contain_text('总 Token');expect(m.locator('#details')).not_to_contain_text('此范围内没有记录');m.reload();expect(m.get_by_role('heading',name='各账号使用情况')).to_be_visible()
 assert not errors,errors
 (R/'full-stack-browser.json').write_text(json.dumps({'passed':True,'built_frontend':True,'restored_revisions':verified,'real_webgl':True,'same_canvas_for_drawers':True,'refresh_retains_revision':True,'monitor_admin_only':True,'monitor_actual_calls_present':True,'page_errors':errors},indent=2));browser.close()
print('BUILT FULL STACK BROWSER PASSED',flush=True)
