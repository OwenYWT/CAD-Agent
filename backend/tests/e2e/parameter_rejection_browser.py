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
 p=ctx.new_page();errors=[];p.on('pageerror',lambda e:errors.append(str(e)))
 try:
  login(p);p.get_by_role('button',name=fixture['title'],exact=False).first.click()
  p.get_by_role('button',name='属性',exact=True).click();tree=p.locator('[data-testid=cloud-document-panel]:visible')
  tree.get_by_role('treeitem',name='Hole',exact=True).click();field=tree.get_by_role('spinbutton',name='Hole.Diameter',exact=True)
  workflows=[]
  for diameter,action in [('7','cancel-plan'),('7.1','reject-candidate')]:
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
    dialog.get_by_role('button',name='拒绝变更',exact=True).click();expect(dialog.get_by_text('审查状态：rejected',exact=True)).to_be_visible(timeout=30000)
    dialog.get_by_role('button',name='关闭',exact=True).last.click()
    expect(field).to_be_enabled(timeout=30000);expect(field).to_have_value(diameter)
    expect(tree.get_by_role('alert').filter(has_text='输入已保留')).to_be_visible()
   current=read(client,'/api/documents/'+fixture['document_id']);assert current['head_revision_id']==before['head_revision_id']
   p.screenshot(path=str(out/(action+'.png')))
  assert workflows[0]!=workflows[1]
  assert not errors,errors
  (out/'report.json').write_text(json.dumps({'passed':True,'workflows':workflows,'saved_head_unchanged':True,'cancelled_input_preserved':True,'rejected_input_preserved':True,'mobile_candidate_action':True,'page_errors':errors},indent=2));print('PARAMETER REJECTION PASSED')
 except BaseException:
  p.screenshot(path=str(out/'failure.png'));(out/'failure.txt').write_text(p.locator('body').inner_text());raise
 finally:ctx.tracing.stop(path=str(out/'trace.zip'));b.close()
