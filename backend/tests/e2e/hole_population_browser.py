"""Real group evidence -> authenticated API -> browser review/commit/reload.

Only planning inputs are controlled. The native kernel, independent inventory,
workflow, database, file storage and HTTP responses run their real code.
"""
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from runtime_fixture import run_native_seed


SEED = '''import asyncio,json,sys
from app.config import settings
from app.db import close_database
from app.domain.identity import user_principal
from app.services.durable_submission import ensure_workspace_identity
from app.temporal_client import get_temporal_client
from app.workflows.temporal import start_mcad_agent_v2_workflow
from app.workers.workflow_worker import build_agent_v2_workflow_worker
from app.execution.composition import get_execution_backend
from app.models.schemas import CADPlan,DesignBrief
from app.contracts.acceptance_v2 import AcceptanceContractV2
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.operation_generator import FreeCADOperationGenerationResult
from tests.constraint_incident import IncidentRequirements,fixture_provenance
from tests.e2e.source_obligation_fixture import bind
class Requirements(IncidentRequirements):
    async def requirements_generation(self,objective,**kwargs):
        return CADPlan(description=objective,part_type='custom',dimensions={},features=[],constraints=[],
            design_brief=DesignBrief(intent_summary=objective,artifact_type='custom',
                acceptance=AcceptanceContractV2.model_validate(bind(objective))))
class Operations:
    async def generate(self,**kwargs):
        operations=[]
        def add(action,**args):
            operations.append(dict(op_id='op-'+str(len(operations)),action=action,args=args))
        add('sketch.create',name='Base')
        add('sketch.add_profile',sketch='Base',geometry=dict(kind='rectangle',corner=dict(x=0,y=0),width_mm=80,height_mm=40))
        add('feature.pad',name='Pad',profile='Base',length_mm=8)
        add('sketch.create',name='Holes',offset_mm=8)
        for index,diameter in enumerate([6,6,4,4]):
            add('sketch.add_profile',sketch='Holes',geometry=dict(kind='circle',center=dict(x=10+index*14,y=20),radius_mm=diameter/2))
        add('feature.pocket',name='HoleCut',profile='Holes',length_mm=8)
        add('document.export',objects=['Body'],formats=['fcstd','step','stl'])
        plan=FreeCADOperationPlan.model_validate(dict(operations=operations));source=plan.model_dump_json()
        return FreeCADOperationGenerationResult(operation_plan=plan,source_code=source,
            generator_kind='controlled-hole-populations',provenance=fixture_provenance(source))
async def main():
    owner=user_principal(sys.argv[1])
    workspace=await ensure_workspace_identity(owner,session_id=sys.argv[2],panel_id=sys.argv[3],title=sys.argv[4],user_id=sys.argv[1])
    settings.temporal_task_queue+='-hole-ui-'+sys.argv[2]
    settings.temporal_agent_v2_task_queue+='-hole-ui-'+sys.argv[2]
    client=await get_temporal_client()
    async with build_agent_v2_workflow_worker(client,backend=get_execution_backend(),
            durable_planner=Requirements(),freecad_operations=Operations()):
        workflow,handle=await start_mcad_agent_v2_workflow(tenant_id=owner.tenant_id,
            project_id=workspace.project_id,principal_id=owner.principal_id,branch_id=workspace.branch_id,
            expected_base_revision_id=workspace.head_revision_id,operation='generate',modeling_backend='freecad',
            objective='板80×40×8mm，2个直径6mm通孔+2个直径4mm通孔。',
            kind='mcad.agent.v2.generate',idempotency_key='hole-browser-'+sys.argv[2],require_worker_ready=False)
        result=await handle.result();assert result['status']=='succeeded',result
    print('HOLE_POPULATION_SEED='+json.dumps(dict(document_id=str(workspace.branch_id),
        base_revision_id=str(workspace.head_revision_id),workflow_id=str(workflow),change_set_id=result['change_set_id'])))
    await close_database()
asyncio.run(main())
'''


def main():
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    out = Path(os.environ['CAD_BROWSER_REPORT_ROOT']) / 'browser-hole-populations'
    if sys.argv[1] == 'seed':
        out.mkdir(parents=True, exist_ok=False)
        session, panel = str(uuid4()), str(uuid4())
        title = 'Mixed hole groups ' + session[:8]
        seed = run_native_seed(SEED, [private['owner']['user']['id'], session, panel, title])
        (out/'seed.log').write_text(seed.stdout + seed.stderr)
        assert seed.returncode == 0, 'real mixed-hole workflow failed; see seed.log'
        fixture = json.loads(next(line.split('=', 1)[1] for line in seed.stdout.splitlines()
                                 if line.startswith('HOLE_POPULATION_SEED=')))
        (out/'fixture.json').write_text(json.dumps({**fixture, 'title': title}, indent=2))
        return
    assert sys.argv[1] == 'browser'
    fixture = json.loads((out/'fixture.json').read_text())
    title = fixture['title']
    errors = []
    with httpx.Client(base_url=os.environ['CAD_NATIVE_E2E_URL'], trust_env=False,
            headers={'Authorization': 'Bearer ' + private['owner']['token']}) as api, sync_playwright() as pw:
        path = '/api/documents/' + fixture['document_id']
        assert api.get(path).raise_for_status().json()['head_revision_id'] == fixture['base_revision_id']
        browser = pw.chromium.launch(headless=True, args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(os.environ['CAD_NATIVE_E2E_WEB'])
            page.locator('#account').fill(private['owner']['phone']); page.locator('#password').fill(private['owner']['password'])
            page.locator('button[type=submit]').click()
            page.get_by_role('button', name=title, exact=False).first.click()
            card = page.locator('[data-testid=authoritative-task]:visible')
            expect(card).to_have_attribute('data-task-phase', 'candidate', timeout=30000)
            card.locator('summary').filter(has_text='检查结果（').click()
            card.get_by_role('button', name='查看检查对象、版本与规则', exact=True).first.click()
            rows = card.get_by_test_id('engineering-measurement')
            expect(rows).to_have_count(11, timeout=30000)
            expect(rows.filter(has_text='本组实测（全范围 4 孔）')).to_have_count(8)
            expect(rows.filter(has_text='本组实测（全范围 4 孔）：2')).to_have_count(2)
            expect(rows.filter(has_text='本组实测（全范围 4 孔）：6、6 mm')).to_have_count(1)
            expect(rows.filter(has_text='本组实测（全范围 4 孔）：4、4 mm')).to_have_count(1)
            page.screenshot(path=str(out/'group-measurements.png'))
            card.get_by_role('button', name='审阅候选与参数变化', exact=True).click()
            review = page.get_by_role('dialog', name='变更审查', exact=True)
            review.get_by_role('textbox', name='审查意见').fill('已核对四孔完整清单、两组数量和通孔证据。')
            review.get_by_role('button', name='应用修改', exact=True).click()
            expect(review.get_by_text('审查状态：committed', exact=True)).to_be_visible(timeout=30000)
            review.get_by_role('button', name='关闭', exact=True).last.click()
            saved = api.get(path).raise_for_status().json()
            assert saved['head_revision_id'] != fixture['base_revision_id']
            page.reload()
            expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision', saved['head_revision_id'], timeout=90000)
            assert not errors, errors
            page.screenshot(path=str(out/'committed-reopened.png'))
            (out/'report.json').write_text(json.dumps({'passed': True, 'fixture': fixture,
                'complete_inventory_visible': True, 'group_measurements_visible': True,
                'candidate_before_commit': True, 'commit_and_reload': True, 'page_errors': errors}, indent=2))
        except BaseException:
            page.screenshot(path=str(out/'failure.png'))
            (out/'failure.txt').write_text(page.locator('body').inner_text())
            raise
        finally:
            context.tracing.stop(path=str(out/'trace.zip'))
            browser.close()
    print('HOLE POPULATION BROWSER PASSED')


if __name__ == '__main__':
    main()
