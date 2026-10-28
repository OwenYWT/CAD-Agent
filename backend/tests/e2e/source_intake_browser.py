"""Homepage original -> saved consent -> real native workflow -> review/reload.

Provider proposals are controlled; no HTTP/WebSocket, CAD, validation or
persistence result is substituted. Physical fit/DFM/vision are not certified.
"""
import asyncio
import json
import os
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, expect
from sqlalchemy import text

from app.db import close_database, tenant_transaction
from app.domain.identity import user_principal
from app.execution.composition import get_execution_backend
from app.temporal_client import get_temporal_client
from app.workers.workflow_worker import build_agent_v2_workflow_worker, build_workflow_worker
from tests.source_intake import OBJECTIVE, SourceIntakeRequirements, SourceIntakeOperations


async def main():
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    out = Path(os.environ['CAD_BROWSER_REPORT_ROOT']) / 'browser-source-intake'
    out.mkdir(parents=True, exist_ok=False)
    owner = user_principal(private['owner']['user']['id'])
    errors = []
    client = await get_temporal_client()
    operations = SourceIntakeOperations()
    async with build_workflow_worker(client, backend=get_execution_backend()), build_agent_v2_workflow_worker(client, backend=get_execution_backend(),
            durable_planner=SourceIntakeRequirements(), freecad_operations=operations), async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        page = await browser.new_page(viewport={'width':1440, 'height':1000})
        await page.context.tracing.start(screenshots=True, snapshots=True)
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto(os.environ['CAD_NATIVE_E2E_WEB'])
            await page.locator('#account').fill(private['owner']['phone'])
            await page.locator('#password').fill(private['owner']['password'])
            await page.locator('button[type=submit]').click()
            await page.get_by_role('button', name='新建设计', exact=True).click()
            await page.get_by_role('button', name=OBJECTIVE, exact=True).click()
            await expect(page.get_by_role('textbox', name='工程需求', exact=True)).to_have_value(OBJECTIVE)
            await page.get_by_role('button', name='开始创建', exact=True).click()
            intake = page.get_by_role('region', name='执行前需求确认', exact=True)
            await expect(intake.get_by_role('button', name='按概念外形继续', exact=True)).to_be_disabled()
            await intake.get_by_role('checkbox', name='确认仅生成概念外形', exact=True).check()
            await intake.get_by_role('button', name='按概念外形继续', exact=True).click()
            card = page.locator('[data-testid=authoritative-task]:visible')
            await expect(card).to_have_attribute('data-task-phase', 'candidate', timeout=300000)
            workflow_id = await card.get_attribute('data-task-id')
            from uuid import UUID
            async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
                saved = await conn.scalar(text('SELECT request_payload FROM workflow_runs WHERE id=:id'), {'id':UUID(workflow_id)})
                report = await conn.scalar(text("SELECT evidence FROM agent_validation_evidence WHERE workflow_run_id=:id AND gate='geometry'"), {'id':UUID(workflow_id)})
            assert saved['objective'] == OBJECTIVE
            assert saved['operation_context']['requirement_basis']['concept_acknowledged'] is True
            assert report['acceptance']['evidence'][0]['measured'] == [25]
            branch = saved['branch_id']
            async with httpx.AsyncClient(base_url=os.environ['CAD_NATIVE_E2E_URL'], trust_env=False,
                    headers={'Authorization':'Bearer ' + private['owner']['token']}) as api:
                response = await api.get('/api/documents/' + branch)
                document = response.raise_for_status().json()
                assert document['head_revision_id'] == saved['expected_base_revision_id']
                await card.locator('summary').filter(has_text='检查结果（').click()
                await card.get_by_role('button', name='查看检查对象、版本与规则', exact=True).first.click()
                source = card.get_by_test_id('engineering-source-context')
                await expect(source).to_contain_text('实物适配、夹持力', timeout=30000)
                await expect(source).to_contain_text('25 mm 桌板')
                await expect(card.get_by_test_id('engineering-measurement')).to_contain_text('≥ 25 mm')
                await page.screenshot(path=str(out/'candidate.png'))
                await card.get_by_role('button', name='审阅候选与参数变化', exact=True).click()
                review = page.get_by_role('dialog', name='变更审查', exact=True)
                await review.get_by_role('textbox', name='审查意见').fill('确认概念夹口净空，实物适配与夹持力尚未验证。')
                await review.get_by_role('button', name='应用修改', exact=True).click()
                await expect(review.get_by_text('审查状态：committed', exact=True)).to_be_visible(timeout=30000)
                await review.get_by_role('button', name='关闭', exact=True).last.click()
                response = await api.get('/api/documents/' + branch)
                committed = response.raise_for_status().json()
                assert committed['head_revision_id'] != document['head_revision_id']
                await page.reload()
                await expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision', committed['head_revision_id'], timeout=90000)
                # Load the persisted evidence again, not React state from before refresh.
                card = page.locator('[data-testid=authoritative-task]:visible')
                await card.locator('summary').filter(has_text='检查结果（').click()
                await card.get_by_role('button', name='查看检查对象、版本与规则', exact=True).first.click()
                await expect(card.get_by_test_id('engineering-source-context')).to_contain_text('实物适配、夹持力', timeout=30000)
            assert operations.calls == 1 and not errors, errors
            await page.screenshot(path=str(out/'committed-reloaded.png'))
            (out/'report.json').write_text(json.dumps({'passed': True, 'workflow_id':workflow_id,
                'homepage_original_submitted':True, 'saved_consent':True, 'actual_gap_mm':25,
                'review_commit_reload':True, 'physical_fit_unverified_retained':True,
                'provider':'controlled-input', 'page_errors':errors}, ensure_ascii=False, indent=2))
        except BaseException:
            await page.screenshot(path=str(out/'failure.png'))
            (out/'failure.txt').write_text(await page.locator('body').inner_text())
            raise
        finally:
            await page.context.tracing.stop(path=str(out/'trace.zip'))
            await browser.close()
    await close_database()
    print('SOURCE INTAKE BROWSER PASSED')


if __name__ == '__main__':
    asyncio.run(main())
