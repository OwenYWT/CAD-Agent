"""Check archived real failure and intake states without retrying the failed task.

CAD_NATIVE_E2E_PRIVATE: existing test credentials. CAD_LAYOUT_FAILED_TITLE:
exact history title of a real failed task without any saved model or artifacts.
CAD_LAYOUT_REPORT_DIR: private output directory. No response substitution.
"""
import json, os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
private=json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text());out=Path(os.environ['CAD_LAYOUT_REPORT_DIR'])
out.mkdir(parents=True,exist_ok=False);out.chmod(0o700)
with sync_playwright() as pw:
 b=pw.chromium.launch(headless=True);p=b.new_page(viewport={'width':1440,'height':1000});errors=[];p.on('pageerror',lambda e:errors.append(str(e)))
 p.goto(os.getenv('CAD_NATIVE_E2E_WEB','http://127.0.0.1:8100'));p.locator('input[type=text]').fill(private['owner']['phone']);p.locator('input[type=password]').fill(private['owner']['password']);p.locator('button[type=submit]').click()
 p.get_by_role('button',name=os.environ['CAD_LAYOUT_FAILED_TITLE'],exact=False).first.click()
 card=p.get_by_test_id('authoritative-task');expect(card).to_have_attribute('data-task-phase','failed',timeout=30000)
 expect(p.get_by_test_id('viewer-task-state')).to_have_attribute('data-task-phase','failed');expect(card.locator('.animate-spin')).to_have_count(0)
 expect(card.get_by_role('button',name='重试本次任务',exact=True)).to_be_enabled();expect(card.get_by_role('button',name='联系管理员',exact=True)).to_be_enabled()
 assert not card.locator('details[open]').count();expect(p.get_by_role('button',name='导出',exact=True)).to_be_disabled()
 p.screenshot(path=str(out/'failed.png'));p.get_by_role('button',name='检查',exact=True).click();expect(p.get_by_test_id('drawer-task-state')).to_have_attribute('data-task-phase','failed')
 p.get_by_role('button',name='返回 Agent',exact=True).click();p.get_by_role('button',name='新建设计',exact=True).click();p.get_by_role('textbox',name='工程需求',exact=True).fill('用于外观评估的手机壳，尺寸未提供');p.get_by_role('button',name='开始创建',exact=True).click()
 intake=p.get_by_role('region',name='执行前需求确认',exact=True);expect(intake).to_be_visible();expect(intake).to_have_attribute('data-task-phase','needs_input');expect(intake.get_by_role('button',name='按概念外形继续',exact=True)).to_be_disabled();expect(intake).to_contain_text('概念外形，适配未验证')
 p.screenshot(path=str(out/'needs-input.png'));assert not errors,errors
 (out/'failure-and-intake.json').write_text(json.dumps({'passed':True,'real_failed_task':True,'failed_animation_stopped':True,'export_disabled_without_artifacts':True,'original_request_retained':True,'intake_requires_basis_confirmation':True,'page_errors':errors},indent=2));b.close();print('failure and intake passed')
