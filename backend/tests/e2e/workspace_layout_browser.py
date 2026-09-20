"""Real workspace layout checks. Run after task_state_browser.py candidate.

Required environment: CAD_NATIVE_E2E_PRIVATE (owner credentials),
CAD_LAYOUT_FIXTURE (the candidate run's fixture.json), CAD_LAYOUT_REPORT_DIR.
CAD_NATIVE_E2E_WEB defaults to the local real frontend. No network interception.
"""
import json, re, os
import httpx
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
private=json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
fixture=json.loads(Path(os.environ['CAD_LAYOUT_FIXTURE']).read_text())
out=Path(os.environ['CAD_LAYOUT_REPORT_DIR']);out.mkdir(parents=True,exist_ok=False);out.chmod(0o700)
with sync_playwright() as pw:
 b=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
 ctx=b.new_context(viewport={'width':1440,'height':1000});ctx.tracing.start(screenshots=True,snapshots=True)
 p=ctx.new_page();errs=[];requests=[];p.on('pageerror',lambda e:errs.append(str(e)));p.on('request',lambda r:requests.append(r.url))
 try:
  p.goto(os.getenv('CAD_NATIVE_E2E_WEB','http://127.0.0.1:8100'));p.locator('input[type=text]').fill(private['owner']['phone']);p.locator('input[type=password]').fill(private['owner']['password']);p.locator('button[type=submit]').click()
  p.get_by_role('button',name=fixture['title'],exact=False).first.click()
  scene=p.locator('[data-testid=document-scene]');expect(scene).to_have_attribute('data-revision',re.compile('.+'),timeout=90000)
  expect(p.locator('.ww-agent-panel:visible')).to_have_count(1);expect(p.locator('.workspace-header__agent-action:visible')).to_have_count(0)
  expect(p.get_by_role('tab',name='BOM',exact=True)).not_to_be_visible()
  assert not any('/api/' in u and ('/bom' in u or '/onshape' in u) for u in requests)
  p.evaluate('window.__layoutCanvas = document.querySelector("[data-testid=document-scene] canvas")')
  canvas=scene.locator('canvas');box=canvas.bounding_box();p.mouse.move(box['x']+box['width']*.45,box['y']+box['height']*.55);p.mouse.down();p.mouse.move(box['x']+box['width']*.52,box['y']+box['height']*.6,steps=15);p.mouse.up();p.wait_for_timeout(1500)
  before=canvas.screenshot()
  for _ in range(20):
   p.wait_for_timeout(750);stable=canvas.screenshot()
   if stable==before: break
   before=stable
  else: raise AssertionError('Orbit camera did not settle before drawer test')
  viewport=p.locator('.ww-primary-pane').bounding_box()
  for name in ['属性','检查','版本','导出','更多工具']:
   p.get_by_role('button',name=name,exact=True).click()
   expect(p.locator('.ww-inspector-pane:visible')).to_have_count(1);expect(p.locator('.ww-agent-panel:visible')).to_have_count(0)
   expect(p.get_by_test_id('drawer-task-state')).to_have_attribute('data-task-phase','saved')
   assert viewport==p.locator('.ww-primary-pane').bounding_box(),name
   assert p.evaluate('window.__layoutCanvas === document.querySelector("[data-testid=document-scene] canvas")')
   p.screenshot(path=str(out/(name+'.png')))
   p.get_by_role('button',name='返回 Agent',exact=True).click()
  after=canvas.screenshot();(out/'camera-before.png').write_bytes(before);(out/'camera-after.png').write_bytes(after)
  assert before==after,'camera changed across drawers'
  p.get_by_role('button',name='属性',exact=True).click();p.keyboard.press('Escape');expect(p.locator('.ww-agent-panel:visible')).to_have_count(1)
  # A real mesh hit must select the same feature in the property tree.
  area=canvas.bounding_box();canvas.click(position={'x':area['width']*.5,'y':area['height']*.62})
  expect(p.locator('.ww-agent-panel:visible')).to_have_count(1)
  expect(p.get_by_test_id('agent-selection')).to_contain_text('整个部件')
  p.get_by_role('button',name='属性',exact=True).click()
  tree=p.locator('[data-testid=cloud-document-panel]:visible')
  selected=tree.get_by_role('treeitem',selected=True);expect(selected).to_have_count(1)
  selected_label=selected.get_attribute('aria-label');expect(tree.locator('[data-feature-properties]')).to_be_visible()
  p.get_by_role('button',name='返回 Agent',exact=True).click()
  expect(p.get_by_test_id('agent-selection')).to_contain_text(selected_label)
  p.get_by_role('button',name='清除本次 AI 选择',exact=True).click()
  p.get_by_role('button',name='属性',exact=True).click();tree.get_by_role('treeitem',name=selected_label,exact=True).click()
  expect(tree.get_by_role('treeitem',name=selected_label,exact=True)).to_have_attribute('aria-selected','true')
  p.get_by_role('button',name='返回 Agent',exact=True).click()
  p.get_by_role('button',name='折叠 Agent',exact=True).click();expect(p.get_by_role('button',name='展开 Agent',exact=True)).to_be_visible()
  p.get_by_role('button',name='展开 Agent',exact=True).click()
  assert p.evaluate('window.__layoutCanvas === document.querySelector("[data-testid=document-scene] canvas")')
  before_fit=len(requests)
  p.get_by_role('button',name='适应视图',exact=True).click();p.wait_for_timeout(750)
  assert p.evaluate('window.__layoutCanvas === document.querySelector("[data-testid=document-scene] canvas")')
  assert not any('/scene' in u or '.stl' in u for u in requests[before_fit:]), 'fit unexpectedly reloaded geometry'
  area=canvas.bounding_box();canvas.dblclick(position={'x':area['width']*.5,'y':area['height']*.62})
  expect(p.locator('.ww-inspector-pane:visible')).to_have_count(1)
  p.get_by_role('button',name='返回 Agent',exact=True).click()
  # A history round trip retains the exact camera, Canvas, LOD and cached geometry.
  p.get_by_role('combobox',name='网格精度',exact=True).select_option('fine')
  expect(scene).to_have_attribute('data-lod','fine',timeout=90000)
  current_revision=scene.get_attribute('data-revision')
  p.get_by_role('button',name='版本',exact=True).click()
  before_history=canvas.screenshot()
  for _ in range(20):
   p.wait_for_timeout(400);stable=canvas.screenshot()
   if stable==before_history: break
   before_history=stable
  # Choose the fixture's original native revision, not a row position: rejected
  # candidates can legitimately insert additional history entries.
  with httpx.Client(base_url=os.getenv('CAD_NATIVE_E2E_URL','http://127.0.0.1:8040'), headers={'Authorization':'Bearer '+private['owner']['token']}) as api:
   response=api.get('/api/change-sets/'+fixture['change_set_id']);response.raise_for_status()
   original_revision=response.json()['candidate_revision_id']
  assert original_revision != current_revision
  p.locator('[data-revision-id="'+original_revision+'"]').get_by_role('button',name='查看此版本',exact=True).click()
  expect(scene).not_to_have_attribute('data-requested-revision',current_revision,timeout=30000)
  history_revision=scene.get_attribute('data-requested-revision')
  expect(scene).to_have_attribute('data-revision',history_revision,timeout=90000)
  assert p.evaluate('window.__layoutCanvas === document.querySelector("[data-testid=document-scene] canvas")')
  expect(scene).to_have_attribute('data-lod','fine')
  previous_requests=len(requests)
  p.get_by_role('button',name='查看已提交版本',exact=True).click()
  expect(scene).to_have_attribute('data-revision',current_revision,timeout=90000)
  p.wait_for_timeout(500)
  assert p.evaluate('window.__layoutCanvas === document.querySelector("[data-testid=document-scene] canvas")')
  expect(scene).to_have_attribute('data-lod','fine')
  after_history=canvas.screenshot()
  (out/'history-before.png').write_bytes(before_history);(out/'history-after.png').write_bytes(after_history)
  assert before_history==after_history,'camera or selection changed after history round trip'
  assert not any('/artifacts/' in u for u in requests[previous_requests:]),'cached scene mesh downloaded again'
  p.get_by_role('button',name='返回 Agent',exact=True).click()
  rows=[]
  for w in [1440,1181,1024,900,760,390,320]:
   p.set_viewport_size({'width':w,'height':900});p.wait_for_timeout(350)
   assert p.evaluate('document.documentElement.scrollWidth <= innerWidth'),w
   for label in ['属性','检查','版本','导出']:
    control=p.get_by_role('button',name=label,exact=True);expect(control).to_be_visible();rect=control.bounding_box();assert 0<=rect['x'] and rect['x']+rect['width']<=w+.5,(w,label,rect)
   if w>760:
    composer=p.locator('.ww-agent-composer-wrap').bounding_box();assert composer['y']+composer['height']<=901,(w,composer)
   p.screenshot(path=str(out/f'width-{w}.png'))
   rows.append({'width':w,'model':p.locator('.ww-primary-pane').bounding_box()})
  p.get_by_role('button',name='属性',exact=True).click();expect(p.get_by_role('dialog',name='工程信息',exact=True)).to_be_visible();p.keyboard.press('Escape');expect(p.get_by_role('dialog',name='工程信息',exact=True)).not_to_be_visible()
  assert not errs,errs
  (out/'report.json').write_text(json.dumps({'status':'passed','widths':rows,'mesh_tree_and_agent_selection':selected_label,'same_canvas':True,'camera_pixels_unchanged':True,'page_errors':errs},ensure_ascii=False,indent=2));print('LAYOUT PASSED',flush=True)
 except BaseException:
  print(p.evaluate("""() => Object.fromEntries(['.workspace-header','.workspace-header__title','.workspace-header__actions'].map(s=>{const e=document.querySelector(s),c=getComputedStyle(e);return [s,{rect:e.getBoundingClientRect().toJSON(),flex:c.flex,minWidth:c.minWidth,width:c.width}]}))"""),flush=True)
  p.screenshot(path=str(out/'failure.png'));(out/'failure.txt').write_text(p.locator('body').inner_text());raise
 finally:ctx.tracing.stop(path=str(out/'trace.zip'));b.close()
