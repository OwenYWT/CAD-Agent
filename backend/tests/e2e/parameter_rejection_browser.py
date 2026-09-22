"""Real native parameter cancellation/rejection and mobile candidate recovery."""
import json,os,time
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright,expect
from task_state_browser import login, terminal, read, API, WEB, PRIVATE
out=Path(os.environ['CAD_PARAMETER_REJECTION_REPORT']);out.mkdir(parents=True,exist_ok=False);out.chmod(0o700)
fixture=json.loads(Path(os.environ['CAD_LAYOUT_FIXTURE']).read_text())
with httpx.Client(base_url=API,timeout=60) as client,sync_playwright() as pw:
 auth=client.post('/api/auth/login/password',json={k:PRIVATE['owner'][k] for k in ('phone','password')});auth.raise_for_status();client.headers['Authorization']='Bearer '+auth.json()['token']
 before=read(client,'/api/documents/'+fixture['document_id'])
 b=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']);ctx=b.new_context(viewport={'width':1440,'height':1000});ctx.tracing.start(screenshots=True,snapshots=True)
 p=ctx.new_page();delayed=[];hold_collaboration=False
 def stream(route):
  server=route.connect_to_server()
  def forward(message):
   if hold_collaboration and isinstance(message,str) and json.loads(message).get('type')=='document_collaboration':
    delayed.append((route,message))
   else:route.send(message)
  server.on_message(forward)
 p.route_web_socket('**/api/documents/'+fixture['document_id']+'/stream*',stream)
 errors=[];p.on('pageerror',lambda e:errors.append(str(e)))
 try:
  login(p);p.get_by_role('button',name=fixture['title'],exact=False).first.click()
  p.get_by_role('button',name='属性',exact=True).click();tree=p.locator('[data-testid=cloud-document-panel]:visible')
  tree.get_by_role('treeitem',name='Hole',exact=True).click();field=tree.get_by_role('spinbutton',name='Hole.Diameter',exact=True)
  workflows=[]
  for diameter,action in [('7','cancel-plan'),('7.1','reject-before-draft'),('7.2','reject-after-draft')]:
   field.fill(diameter)
   with p.expect_response(lambda r:r.url.endswith('/operations') and r.request.method=='POST') as response:tree.get_by_role('button',name='提交参数变更',exact=True).click()
   assert response.value.status==202,response.value.text()
   workflow=response.value.json()['workflow_run_id'];workflows.append(workflow)
   snapshot=terminal(client,p,workflow)
   assert snapshot['status']=='waiting_confirmation',snapshot['status']
   confirm=tree.get_by_role('region',name='参数执行计划确认',exact=True)
   if action=='cancel-plan':
    confirm.get_by_role('button',name='拒绝执行计划',exact=True).click()
    expect(confirm).not_to_be_visible(timeout=30000)
    expect(field).to_be_enabled(timeout=60000);expect(field).to_have_value(diameter)
    expect(tree.get_by_role('alert').filter(has_text='输入已保留')).to_be_visible()
   else:
    confirm.get_by_role('button',name='确认并继续',exact=True).click();expect(confirm).not_to_be_visible(timeout=30000)
    snapshot=terminal(client,p,workflow);assert snapshot['status']=='succeeded',snapshot.get('error_message')
    expect(tree.get_by_role('button',name='查看变更 / 应用修改',exact=True)).to_be_visible(timeout=30000)
    p.set_viewport_size({'width':390,'height':844});p.get_by_role('button',name='返回 Agent',exact=True).click()
    p.get_by_role('button',name='预览',exact=True).click()
    notice=p.get_by_role('button',name='模型已生成，候选待确认 · 点击处理',exact=True);expect(notice).to_be_visible();notice.click()
    expect(p.get_by_test_id('authoritative-task')).to_be_visible()
    p.set_viewport_size({'width':1440,'height':1000});p.get_by_role('button',name='属性',exact=True).click()
    tree.get_by_role('button',name='查看变更 / 应用修改',exact=True).click()
    dialog=p.get_by_role('dialog',name='变更审查',exact=True)
    dialog.get_by_role('textbox',name='审查意见',exact=True).fill('验收拒绝路径：保留原始 6.5 mm 孔径。')
    # Observe every guard request, including one that only flashes between frames.
    p.evaluate('''async () => {
      const {useDraftGuardStore} = await import('/src/stores/draftGuard.ts');
      window.__unexpectedDraftGuards = 0;
      window.__draftState = () => useDraftGuardStore.getState();
      window.__stopDraftObserver = useDraftGuardStore.subscribe((state, previous) => {
        if (state.pending && state.pending !== previous.pending) window.__unexpectedDraftGuards++;
      });
    }''')
    hold_collaboration=action=='reject-before-draft'
    refreshes=[]
    def refresh(route):
     response=route.fetch()  # Real response; delay delivery only to exercise ordering.
     if action=='reject-after-draft':
      p.wait_for_function('Object.keys(window.__draftState().drafts).length > 0')
      expect(field).to_be_enabled();expect(field).to_have_value(diameter)
     refreshes.append(True)
     route.fulfill(response=response)
    url=WEB.rstrip('/')+'/api/documents/'+fixture['document_id']
    p.route(url,refresh)
    dialog.get_by_role('button',name='拒绝变更',exact=True).click()
    expect(dialog.get_by_text('审查状态：rejected',exact=True)).to_be_visible(timeout=30000)
    expect(dialog.get_by_role('button',name='拒绝变更',exact=True)).to_be_visible(timeout=30000)
    # The review action remains restoring until refresh and its callback finish.
    expect(dialog.get_by_role('button',name='关闭',exact=True).last).to_be_enabled()
    if action=='reject-before-draft':
     assert p.evaluate('Object.keys(window.__draftState().drafts).length')==0
     assert delayed, 'no actual collaboration messages were delayed'
     hold_collaboration=False
     for route,message in delayed:route.send(message)
     delayed.clear()
    p.wait_for_function('Object.keys(window.__draftState().drafts).length > 0')
    p.wait_for_function('document.querySelector("[role=dialog]") !== null')
    assert refreshes, 'review did not refresh the actual document'
    # Flush React effects after the actual document response has been consumed.
    p.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    assert p.evaluate('window.__unexpectedDraftGuards')==0, 'background refresh opened the draft navigation guard'
    p.unroute(url,refresh)
    p.evaluate('window.__stopDraftObserver()')
    dialog.get_by_role('button',name='关闭',exact=True).last.click()
    expect(field).to_be_enabled(timeout=30000);expect(field).to_have_value(diameter)
    expect(tree.get_by_role('alert').filter(has_text='输入已保留')).to_be_visible()
   current=read(client,'/api/documents/'+fixture['document_id']);assert current['head_revision_id']==before['head_revision_id']
   p.screenshot(path=str(out/(action+'.png')))
  # Same-version navigation is a no-op, but genuine history navigation must
  # still ask before leaving the restored dirty parameter draft.
  original=read(client,'/api/change-sets/'+fixture['change_set_id'])['candidate_revision_id']
  assert original != before['head_revision_id']
  p.get_by_role('button',name='版本',exact=True).click()
  p.locator('[data-revision-id="'+before['head_revision_id']+'"]').get_by_role('button',name='查看此版本',exact=True).click()
  expect(p.get_by_role('button',name='放弃草稿并切换',exact=True)).not_to_be_visible()
  p.locator('[data-revision-id="'+original+'"]').get_by_role('button',name='查看此版本',exact=True).click()
  expect(p.get_by_role('button',name='放弃草稿并切换',exact=True)).to_be_visible()
  p.get_by_role('button',name='继续编辑',exact=True).click()
  expect(p.get_by_role('button',name='放弃草稿并切换',exact=True)).not_to_be_visible()
  p.get_by_role('button',name='属性',exact=True).click()
  expect(field).to_have_value('7.2')
  assert read(client,'/api/documents/'+fixture['document_id'])['head_revision_id']==before['head_revision_id']
  assert len(set(workflows))==3
  assert not errors,errors
  (out/'report.json').write_text(json.dumps({'passed':True,'workflows':workflows,'saved_head_unchanged':True,'cancelled_input_preserved':True,'rejected_input_preserved':True,'mobile_candidate_action':True,'rejection_refresh_orders':['refresh-before-draft','draft-before-refresh'],'no_extra_draft_prompt':True,'same_revision_no_guard':True,'real_history_navigation_guarded':True,'page_errors':errors},indent=2));print('PARAMETER REJECTION PASSED')
 except BaseException:
  p.screenshot(path=str(out/'failure.png'));(out/'failure.txt').write_text(p.locator('body').inner_text());raise
 finally:ctx.tracing.stop(path=str(out/'trace.zip'));b.close()
