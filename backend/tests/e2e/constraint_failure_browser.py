"""Actual native failure and repair history remains understandable in the UI.

Only provider proposals are controlled; HTTP, Temporal, FreeCAD, evidence and
the browser use the production implementations. No responses are intercepted.
"""
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from runtime_fixture import run_native_seed

SEED = '''import asyncio,json,sys
from uuid import uuid4
from temporalio.client import WorkflowFailureError
from app.config import settings
from app.db import close_database
from app.domain.identity import user_principal
from app.services.durable_submission import ensure_workspace_identity
from app.temporal_client import get_temporal_client
from app.workflows.temporal import start_mcad_agent_v2_workflow
from app.workflows.agent_v2 import McadAgentWorkflowV2
from app.workers.workflow_worker import build_agent_v2_workflow_worker
from app.execution.composition import get_execution_backend
from tests.constraint_incident import IncidentRequirements,IncidentGenerator
async def main():
    owner=user_principal(sys.argv[1])
    workspace=await ensure_workspace_identity(owner,session_id=sys.argv[2],panel_id=sys.argv[3],title=sys.argv[4],user_id=sys.argv[1])
    settings.temporal_task_queue+='-repair-ui-'+sys.argv[2]
    settings.temporal_agent_v2_task_queue+='-repair-ui-'+sys.argv[2]
    objective='设计一个可夹在 25 mm 桌板上的耳机挂钩，最终为一个连通实体'
    client=await get_temporal_client()
    async with build_agent_v2_workflow_worker(client,backend=get_execution_backend(),
            durable_planner=IncidentRequirements(),freecad_operations=IncidentGenerator(connected_fixture=False)):
        # Persist the same complete document/session association as admission.
        # This fixture already owns the worker; poller metrics can lag startup.
        workflow_id,handle=await start_mcad_agent_v2_workflow(tenant_id=owner.tenant_id,
            project_id=workspace.project_id,principal_id=owner.principal_id,branch_id=workspace.branch_id,
            expected_base_revision_id=workspace.head_revision_id,operation='generate',modeling_backend='freecad',
            objective=objective,kind='mcad.agent.v2.generate',idempotency_key='constraint-browser-'+sys.argv[2],
            require_worker_ready=False)
        try: await handle.result()
        except WorkflowFailureError as exc:
            cause=McadAgentWorkflowV2._root_application_error(exc)
            assert cause.type=='profile_replan_rejected',cause
            assert 'earlier constraint proof' in str(cause),cause
        else: raise AssertionError('self-intersecting incident must not succeed')
    print('CONSTRAINT_FAILURE='+json.dumps({'document_id':str(workspace.branch_id),
        'base_revision_id':str(workspace.head_revision_id),'workflow_id':str(workflow_id)}))
    await close_database()
asyncio.run(main())
'''


def main():
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    out = Path(os.environ['CAD_CONSTRAINT_BROWSER_REPORT']); out.mkdir(parents=True, exist_ok=False)
    session, panel = str(uuid4()), str(uuid4())
    title = 'Constraint repair history ' + session[:8]
    seed = run_native_seed(SEED, [private['owner']['user']['id'], session, panel, title])
    (out/'seed.log').write_text(seed.stdout + seed.stderr)
    assert seed.returncode == 0, 'real failed workflow fixture did not complete; see seed.log'
    fixture = json.loads(next(line.split('=', 1)[1] for line in seed.stdout.splitlines()
                             if line.startswith('CONSTRAINT_FAILURE=')))
    with httpx.Client(base_url=os.environ['CAD_NATIVE_E2E_URL'],
                      headers={'Authorization': 'Bearer ' + private['owner']['token']}) as api:
        response = api.get('/api/tasks/' + fixture['workflow_id'] + '/snapshot'); response.raise_for_status()
        snapshot = response.json()
        assert snapshot['status'] == 'failed' and not snapshot.get('change_set')
        response=api.get('/api/tasks/'+fixture['workflow_id']+'/diagnostic');response.raise_for_status()
        diagnostic=response.json()
        assert diagnostic['snapshot']['valid_checkpoint'] is False and diagnostic['task_status']=='failed'
        assert diagnostic['snapshot']['failed_operation_id'] and diagnostic['snapshot']['sketches']
        assert all('solver' in sketch and 'geometry' in sketch and 'constraints' in sketch for sketch in diagnostic['snapshot']['sketches'])
        assert all(operation['status'] in {'executed','failing','not_executed'} for operation in diagnostic['snapshot']['operations'])
        for headers in [{},{'Authorization':'Bearer '+private['editor']['token']}]:
            for route in ['diagnostic','takeover-baseline']:
                denied=httpx.get(os.environ['CAD_NATIVE_E2E_URL']+'/api/tasks/'+fixture['workflow_id']+'/'+route,headers=headers,trust_env=False)
                assert denied.status_code in {401,403,404},(route,denied.text)
        response = api.get('/api/documents/' + fixture['document_id']); response.raise_for_status()
        assert response.json()['head_revision_id'] == fixture['base_revision_id']
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(os.environ['CAD_NATIVE_E2E_WEB'])
            page.locator('input[type=text]').fill(private['owner']['phone'])
            page.locator('input[type=password]').fill(private['owner']['password'])
            page.locator('button[type=submit]').click()
            page.get_by_role('button', name=title, exact=False).first.click()
            card = page.locator('[data-testid=authoritative-task]:visible')
            expect(card).to_have_attribute('data-task-phase', 'failed')
            chain = card.get_by_test_id('repair-failure-chain')
            expect(chain).to_be_visible()
            chain.locator('summary').click()
            expect(chain).to_contain_text('原始建模错误：')
            expect(chain).to_contain_text('redundant')
            expect(chain).to_contain_text('修复失败原因：')
            expect(chain).to_contain_text('profile cannot support a valid downstream feature')
            expect(chain).to_contain_text('earlier constraint proof')
            expect(card.locator('.animate-spin')).to_have_count(0)
            card.get_by_role('button',name='查看失败草图与求解诊断',exact=True).click()
            diagnostic_dialog=page.get_by_role('dialog',name='失败现场诊断',exact=True)
            expect(diagnostic_dialog.get_by_role('group',name='失败草图几何',exact=True).first).to_be_visible()
            expect(diagnostic_dialog).to_contain_text('自由度：')
            for _ in range(12):
                page.keyboard.press('Tab');assert diagnostic_dialog.evaluate('e=>e.contains(document.activeElement)')
            diagnostic_dialog.get_by_role('button',name='关闭',exact=True).click()
            page.reload()
            expect(page.get_by_test_id('repair-failure-chain')).to_be_visible()
            assert not errors, errors
            page.screenshot(path=str(out/'retained-failure.png'))
            (out/'report.json').write_text(json.dumps({'passed': True, 'fixture': fixture,
                'real_failed_workflow': True, 'original_and_repair_error_visible': True,
                'no_candidate': True, 'head_unchanged': True, 'reload_preserved': True,
                'owned_diagnostic':True,'diagnostic_permissions':True,'diagnostic_focus_trap':True,'page_errors': errors}, indent=2))
            print('CONSTRAINT FAILURE BROWSER PASSED')
        except BaseException:
            page.screenshot(path=str(out/'failure.png'))
            (out/'failure.txt').write_text(page.locator('body').inner_text())
            raise
        finally:
            browser.close()


if __name__ == '__main__':
    main()
