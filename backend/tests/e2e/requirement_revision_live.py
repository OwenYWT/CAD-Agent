"""One real paid original/revised requirement lifecycle through the product UI.

Run only behind the authorized budget gateway. No model or HTTP substitutions.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import httpx
from playwright.sync_api import expect, sync_playwright


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def main():
    assert os.environ.get('CAD_C10_LIVE_AUTHORIZED') == '1', 'explicit bounded live authorization required'
    assert os.environ['LLM_BASE_URL'].startswith('http://127.0.0.1:'), 'budget gateway required'
    root = Path(__file__).resolve().parents[3]
    out = Path(os.environ['CAD_C10_REPORT'])
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    errors, runs = [], []
    with httpx.Client(base_url=os.environ['CAD_NATIVE_E2E_URL'], timeout=60, trust_env=False,
        headers={'Authorization':'Bearer '+private['owner']['token']}) as api, sync_playwright() as pw:
        def read(path):
            return api.get(path).raise_for_status().json()
        browser = pw.chromium.launch(headless=True,args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        context = browser.new_context(viewport={'width':1440,'height':1000})
        context.tracing.start(screenshots=True,snapshots=True)
        page = context.new_page()
        page.on('pageerror',lambda error: errors.append(str(error)))
        def collect(task):
            snapshot=read('/api/tasks/'+task+'/snapshot')
            events=[]; sequence=0
            while True:
                value=read(f'/api/tasks/{task}/events?after_sequence={sequence}&limit=500')
                batch=value['events']
                if not batch: break
                events.extend(batch); sequence=batch[-1]['sequence']
                if len(batch)<500: break
            return snapshot,events
        def wait_candidate(task, name):
            confirmed=False
            for _ in range(1200):
                snapshot=read('/api/tasks/'+task+'/snapshot')
                (out/(name+'-latest.json')).write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
                if snapshot['status']=='waiting_confirmation' and not confirmed:
                    group=page.get_by_role('group',name='服务端执行计划确认',exact=True)
                    expect(group).to_be_visible()
                    group.get_by_role('button',name='确认并继续',exact=True).click()
                    confirmed=True
                if snapshot['status'] in {'succeeded','failed','cancelled','timed_out'}:
                    assert snapshot['status']=='succeeded', snapshot.get('error') or snapshot.get('error_message')
                    assert snapshot['change_set'] and snapshot['change_set']['status']=='pending_review'
                    return snapshot
                page.wait_for_timeout(500)
            raise AssertionError('Real requirement workflow did not reach a candidate')
        def commit(snapshot,name,thickness):
            task=snapshot['id']; document=snapshot['request_payload']['branch_id']
            path='/api/documents/'+document
            before=read(path)
            assert before['head_revision_id']==snapshot['request_payload']['expected_base_revision_id']
            for artifact in snapshot['artifacts']:
                if artifact['artifact_kind'] in {'fcstd','step'}:
                    response=api.get(artifact['download_url']).raise_for_status()
                    assert hashlib.sha256(response.content).hexdigest()==artifact['sha256']
                    suffix='.FCStd' if artifact['artifact_kind']=='fcstd' else '.step'
                    (out/(name+suffix)).write_bytes(response.content)
            plan=snapshot['agent']['plan']
            acceptance=plan['design_brief']['acceptance']
            assert not acceptance.get('unresolved') and not acceptance.get('verification_limits')
            checks=acceptance['checks']
            dims={tuple(abs(x) for x in c['scope']['axis']):c['nominal'] for c in checks if c['kind']=='overall_dimension' and c['required']}
            assert dims[(1,0,0)]==60 and dims[(0,1,0)]==40 and dims[(0,0,1)]==thickness,dims
            for kind,nominal in [('solid_count',1),('hole_count',1),('hole_diameter',12),('hole_depth',thickness)]:
                assert any(c['kind']==kind and c['nominal']==nominal and c['required'] for c in checks),(kind,checks)
            assert any(c['kind']=='hole_position' and c['required'] for c in checks)
            validations=[]
            for item in snapshot['agent']['validations']:
                value=read(f'/api/tasks/{task}/validations/'+item['evidence_id'])
                if value['selected_for_revision'] and item['mode']=='required':
                    assert item['outcome']=='passed',value
                validations.append(value)
            assert any(v['selected_for_revision'] and v['gate']=='geometry' and v['outcome']=='passed' for v in validations)
            card=page.get_by_test_id('authoritative-task')
            expect(card).to_have_attribute('data-task-phase','candidate')
            card.get_by_role('button',name='审阅候选与参数变化',exact=True).click()
            review=page.get_by_role('dialog',name='变更审查',exact=True)
            review.get_by_role('textbox',name='审查意见',exact=True).fill('核对明确尺寸、必需检查及版本归属；导出文件将由独立 FreeCAD 重开测量。')
            review.get_by_role('button',name='应用修改',exact=True).click()
            expect(review.get_by_text('审查状态：committed',exact=True)).to_be_visible(timeout=30000)
            review.get_by_role('button',name='关闭',exact=True).last.click()
            saved=read(path)
            assert saved['head_revision_id']==snapshot['change_set']['candidate_revision_id']
            assert saved['state_version']==before['state_version']+1
            page.reload()
            expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision',saved['head_revision_id'],timeout=90000)
            frozen,events=collect(task)
            requirements=[e for e in events if e['event_type']=='agent.requirements.completed']
            assert len(requirements)==1
            record={'task':task,'document':document,'revision':saved['head_revision_id'],
                'request':frozen['request_payload'],'requirements':requirements[0], 'plan':plan,
                'acceptance':acceptance,'acceptance_sha256':digest(acceptance),'validations':validations,
                'snapshot':frozen,'events':events,'revision_view':read(path+'/revisions/'+saved['head_revision_id'])}
            (out/(name+'-frozen.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2))
            page.screenshot(path=str(out/(name+'-reopened.png')))
            runs.append(record)
            return record
        try:
            page.goto(os.environ['CAD_NATIVE_E2E_WEB'],wait_until='domcontentloaded')
            page.locator('#account').fill(private['owner']['phone']);page.locator('#password').fill(private['owner']['password'])
            page.locator('button[type=submit]').click()
            prompt='创建一个单实体矩形板，X 方向长度 60 mm，Y 方向宽度 40 mm，Z 方向厚度 9 mm。板中心有且只有一个直径 12 mm、沿 Z 方向贯穿全厚度的圆孔。只验收上述几何。'
            page.get_by_role('textbox',name='工程需求',exact=True).fill(prompt)
            page.get_by_role('combobox',name='制造方式',exact=True).select_option('generic')
            page.get_by_role('button',name='开始创建',exact=False).click()
            card=page.get_by_role('region',name='执行前需求确认',exact=True)
            card.get_by_role('combobox',name='建模范围',exact=True).select_option('geometry')
            card.get_by_role('textbox',name='关键尺寸依据',exact=True).fill('X 长度 60 mm；Y 宽度 40 mm；Z 厚度 9 mm；中心贯穿孔直径 12 mm，孔数 1；实体数 1')
            card.get_by_role('button',name='确认并执行',exact=True).click()
            task_card=page.get_by_test_id('authoritative-task')
            expect(task_card).to_have_attribute('data-task-id',re.compile(r'^[0-9a-f-]{36}$'),timeout=30000)
            task=task_card.get_attribute('data-task-id')
            original=commit(wait_candidate(task,'original'),'original',9)
            revised_prompt='将板厚修改为 10 mm，其他要求保持：X 方向长度 60 mm，Y 方向宽度 40 mm，Z 方向厚度 10 mm；单实体；有且只有一个位于板中心、直径 12 mm、沿 Z 方向贯穿全厚度的圆孔。只验收上述几何。'
            page.get_by_role('textbox',name='询问 Agent',exact=True).fill(revised_prompt)
            page.get_by_role('button',name='审查请求',exact=True).click()
            page.get_by_role('button',name='修改需求尺寸或依据',exact=True).click()
            card=page.get_by_role('region',name='执行前需求确认',exact=True)
            card.get_by_role('combobox',name='建模范围',exact=True).select_option('geometry')
            dimensions='X 长度 60 mm；Y 宽度 40 mm；Z 厚度 10 mm；中心贯穿孔直径 12 mm，孔数 1；实体数 1'
            card.get_by_role('textbox',name='关键尺寸依据',exact=True).fill(dimensions)
            card.get_by_role('button',name='确认并执行',exact=True).click()
            expect(task_card).not_to_have_attribute('data-task-id',task)
            expect(task_card).to_have_attribute('data-task-id',re.compile(r'^[0-9a-f-]{36}$'))
            modified=task_card.get_attribute('data-task-id')
            revised=commit(wait_candidate(modified,'revised'),'revised',10)
            assert revised['request']['operation']=='modify'
            assert revised['request']['expected_base_revision_id']==original['revision']
            assert revised['request']['operation_context']['requirement_basis']['dimensions']==dimensions
            assert original['acceptance_sha256']!=revised['acceptance_sha256']
            original_now,events_now=collect(original['task'])
            assert original_now['request_payload']==original['request']
            assert events_now==original['events']
            assert original_now['agent']['plan']==original['plan']
            view=read('/api/documents/'+original['document']+'/revisions/'+original['revision'])
            volatile={'head_revision_id','head_state_version'}
            assert {k:v for k,v in view.items() if k not in volatile}=={k:v for k,v in original['revision_view'].items() if k not in volatile}
            for artifact in original['snapshot']['artifacts']:
                response=api.get(artifact['download_url']).raise_for_status()
                assert hashlib.sha256(response.content).hexdigest()==artifact['sha256']
            for evidence in original['validations']:
                assert read(f'/api/tasks/{original["task"]}/validations/'+evidence['evidence_id'])==evidence
            command=[os.environ.get('SANDBOX_COMMAND','docker'),'run','--rm','--network','none','--read-only',
                '--cap-drop','ALL','--security-opt','no-new-privileges','--tmpfs','/tmp:rw,size=2g',
                '-v',str(out)+':/measurements:ro','-v',str(root/'backend/tests/e2e')+':/tests:ro',
                '--entrypoint','/opt/freecad/bin/FreeCADCmd',os.environ['SANDBOX_IMAGE'],'-c',
                "exec(compile(open('/tests/requirement_revision_measurements.py').read(),'/tests/requirement_revision_measurements.py','exec'))"]
            measured=subprocess.run(command,capture_output=True,text=True,check=False)
            (out/'kernel.log').write_text(measured.stdout+measured.stderr)
            assert measured.returncode==0 and 'Traceback (most recent call last)' not in measured.stdout+measured.stderr
            geometry=json.loads(next(line.split('=',1)[1] for line in measured.stdout.splitlines() if line.startswith('CAD_REQUIREMENT_REVISION_MEASUREMENTS=')))
            assert geometry['passed'] and len(geometry['measurements'])==4
            assert not errors,errors
            report={'passed':True,'classification':'REAL_BROWSER_PROVIDER_TEMPORAL_FREECAD_SAVE_REOPEN',
                'natural_language_geometry_evaluated':True,'tasks':[r['task'] for r in runs],
                'revisions':[r['revision'] for r in runs],'acceptance_hashes':[r['acceptance_sha256'] for r in runs],
                'original_frozen_records_and_artifacts_unchanged':True,'geometry':geometry,'page_errors':errors}
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        except BaseException:
            page.screenshot(path=str(out/'failure.png'))
            (out/'failure.txt').write_text(page.locator('body').inner_text())
            raise
        finally:
            context.tracing.stop(path=str(out/'trace.zip'));browser.close()


if __name__=='__main__': main()
