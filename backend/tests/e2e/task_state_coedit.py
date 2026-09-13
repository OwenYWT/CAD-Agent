"""Real Agent edit of the same hole and a retained manual draft during execution."""
import json
import os
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect,sync_playwright
from task_state_browser import API,WEB,OUT,PRIVATE,login,read,card,consistent,save,review
from task_state_controls import open_task_session


def main():
    OUT.mkdir(parents=True,exist_ok=False);OUT.chmod(0o700)
    source=os.environ['CAD_EVIDENCE_WORKFLOW']
    errors=[]
    with httpx.Client(base_url=API,timeout=60,headers={'Authorization':'Bearer '+PRIVATE['owner']['token']}) as client,sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        context=browser.new_context(viewport={'width':1440,'height':1000});context.tracing.start(screenshots=True,snapshots=True)
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        try:
            login(page);source_task=open_task_session(page,client,source)
            doc_id=source_task['request_payload']['branch_id']
            before=read(client,'/api/documents/'+doc_id)
            params=lambda doc:{p['id']:p['value'] for f in doc['features'] for p in f['parameters']}
            old=params(before);target=old['Hole.Diameter']+0.5
            panel=page.get_by_test_id('cloud-document-panel')
            panel.get_by_role('treeitem',name='Hole',exact=True).click()
            expect(page.get_by_test_id('agent-selection')).to_contain_text('Hole')
            prompt=f'将选中的 Hole 特征的 Diameter 从 {old["Hole.Diameter"]:g} mm 扩大 0.5 mm 到 {target:g} mm。保持已提交模型的其他全部参数、板外形和孔位置不变，不重建特征。'
            page.get_by_role('textbox',name='询问 Agent',exact=True).fill(prompt)
            page.get_by_role('button',name='审查请求',exact=True).click()
            page.get_by_role('button',name='确认并执行',exact=True).click()
            consistent(page,'running')
            expect(card(page)).not_to_have_attribute('data-task-id',source,timeout=30000)
            expect(card(page)).to_have_attribute('data-task-id',__import__('re').compile('.+'),timeout=30000)
            workflow=card(page).get_attribute('data-task-id')
            (OUT/'submitted.json').write_text(json.dumps({'workflow_id':workflow,'document_id':doc_id,'original_parameters':old,'target_diameter':target},indent=2))
            # A manual edit starts after the Agent target was frozen.
            panel.get_by_role('treeitem',name='Pad',exact=True).click()
            field=panel.get_by_role('spinbutton',name='Pad.Length',exact=True)
            draft=old['Pad.Length']+0.4;field.fill(str(draft))
            expect(panel.get_by_text('我的未提交修改',exact=True)).to_be_visible()
            save(page,'my-draft-during-agent')
            deadline=time.monotonic()+900;confirmed=False
            while time.monotonic()<deadline:
                result=read(client,f'/api/tasks/{workflow}/snapshot')
                if result['status']=='waiting_confirmation' and not confirmed:
                    page.get_by_role('group',name='服务端执行计划确认',exact=True).get_by_role('button',name='确认并继续',exact=True).click();confirmed=True
                if result['status'] in ['succeeded','failed','cancelled','timed_out']:break
                page.wait_for_timeout(1000)
            (OUT/'snapshot.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
            assert result['status']=='succeeded',{k:result.get(k) for k in ['status','error_code','error_message']}
            selection=result['request_payload']['operation_context']['selection_context']
            hole=next(f for f in before['features'] if f['kernel_name']=='Hole')
            assert selection['feature_ids']==[hole['id']] and selection['revision_id']==before['head_revision_id']
            consistent(page,'candidate');expect(field).to_have_value(str(draft))
            assert read(client,'/api/documents/'+doc_id)['head_revision_id']==before['head_revision_id']
            save(page,'candidate-preserves-my-draft')
            panel.get_by_role('button',name='放弃草稿并读取当前参数',exact=True).click()
            guard=page.get_by_role('dialog',name='保留编辑草稿')
            if guard.is_visible():guard.get_by_role('button',name='放弃草稿并切换',exact=True).click()
            review(page)
            after=read(client,'/api/documents/'+doc_id)
            assert params(after)=={**old,'Hole.Diameter':target}
            assert {f['id'] for f in after['features']}=={f['id'] for f in before['features']}
            assert after['state_version']==before['state_version']+1
            page.reload();consistent(page,'saved');save(page,'ai-hole-edit-saved')
            assert not errors,errors
            report={'status':'passed','workflow_id':workflow,'document_id':doc_id,'selection':selection,
                'hole_before_mm':old['Hole.Diameter'],'hole_after_mm':target,'other_parameters_unchanged':True,
                'manual_draft_preserved_during_agent_and_candidate':True,'draft_explicitly_discarded':True,
                'stable_feature_ids':True,'page_errors':errors}
            (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print('CAD_TASK_COEDIT='+json.dumps(report,ensure_ascii=False))
        except BaseException:save(page,'failure');raise
        finally:context.tracing.stop(path=str(OUT/'trace.zip'));browser.close()


if __name__=='__main__':main()
