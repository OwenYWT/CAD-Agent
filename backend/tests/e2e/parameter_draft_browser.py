"""Component-only state regression. Start the production Vite dev server first.
No CAD execution or API responses are simulated; this page cannot submit tasks.
CAD_PARAMETER_QA_URL overrides the QA entry URL.
"""
import os
from playwright.sync_api import sync_playwright,expect
with sync_playwright() as pw:
 b=pw.chromium.launch(headless=True);p=b.new_page();errors=[];p.on('pageerror',lambda e:errors.append(str(e)));p.goto(os.getenv('CAD_PARAMETER_QA_URL','http://127.0.0.1:5173/qa/parameter-draft.html'));p.get_by_role('button',name='加载参数',exact=True).click();p.get_by_role('button',name='打开属性',exact=True).click();field=p.get_by_role('spinbutton');expect(field).to_have_value('6.5');expect(p.get_by_role('button',name='保存参数',exact=True)).to_be_disabled();field.fill('6.8');p.get_by_role('button',name='取消',exact=True).click();p.get_by_role('button',name='打开属性',exact=True).click();expect(field).to_have_value('6.8');p.get_by_role('button',name='恢复默认值',exact=True).click();expect(field).to_have_value('6.5');p.get_by_role('checkbox',name='只读',exact=True).check();expect(field).to_be_disabled();expect(p.get_by_role('button',name='当前版本只读',exact=True)).to_be_disabled();assert not errors,errors;b.close();print('parameter component: delayed data, unchanged baseline, draft retention and reset passed')
