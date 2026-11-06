"""Browser input -> production planner admission -> real CAD -> commit/reload.

Only provider proposals are controlled. No API, workflow, validation, storage,
review or scene response is replaced. This is not a live AI benchmark.
"""
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

import httpx
from playwright.async_api import async_playwright, expect
from sqlalchemy import text

from app.db import close_database, tenant_transaction
from app.domain.identity import user_principal
from app.execution.composition import get_execution_backend
from app.temporal_client import get_temporal_client
from app.workers.workflow_worker import build_agent_v2_workflow_worker, build_workflow_worker
from tests.intake_semantics import intake_case, IntakeSemanticsRequirements, IntakeSemanticsOperations
from tests.rectangular_assembly import assembly_case, AssemblyRequirements, AssemblyOperations


async def verify_preview_preserves_input(page):
    # Delay the real request until the user has focused and selected the field.
    # No response is replaced: this exercises the browser/API scheduling race.
    objective, basis = intake_case('cube')
    arrived, release = asyncio.Event(), asyncio.Event()
    preview_url = '**/api/tasks/requirements/preview'

    async def delay_preview(route):
        arrived.set()
        await release.wait()
        await route.continue_()

    await page.route(preview_url, delay_preview)
    try:
        await page.get_by_role('button', name='新建设计', exact=True).click()
        await page.get_by_role('textbox', name='工程需求', exact=True).fill(objective)
        await page.get_by_role('button', name='开始创建', exact=True).click()
        await asyncio.wait_for(arrived.wait(), timeout=30)
        intake = page.get_by_role('region', name='执行前需求确认', exact=True)
        dimensions = intake.get_by_role('textbox', name='关键尺寸依据', exact=True)
        await expect(dimensions).to_have_value('')
        await dimensions.select_text()
        async with page.expect_response(preview_url) as pending:
            release.set()
        response = await pending.value
        assert response.status == 200
        assert await response.json() == {'dimensions': basis.dimensions, 'complete': True}
        await page.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        await page.keyboard.insert_text(basis.dimensions)
        await expect(dimensions).to_have_value(basis.dimensions)
        await expect(intake.get_by_role('checkbox', name='确认仅生成概念外形', exact=True)).not_to_be_checked()
    finally:
        release.set()
        await page.unroute(preview_url, delay_preview)


async def main():
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    out = Path(os.environ['CAD_BROWSER_REPORT_ROOT'])/'browser-intake-semantics'
    out.mkdir(parents=True, exist_ok=False)
    owner = user_principal(private['owner']['user']['id'])
    errors, cases = [], []
    client = await get_temporal_client()
    async with build_workflow_worker(client, backend=get_execution_backend()), async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page = await browser.new_page(viewport={'width':1440,'height':1000})
        await page.context.tracing.start(screenshots=True,snapshots=True)
        page.on('pageerror',lambda error: errors.append(str(error)))
        try:
            await page.goto(os.environ['CAD_NATIVE_E2E_WEB'])
            await page.locator('#account').fill(private['owner']['phone'])
            await page.locator('#password').fill(private['owner']['password'])
            await page.locator('button[type=submit]').click()
            await verify_preview_preserves_input(page)
            for name in ('fdm','diameter','assembly','passage','cube','cube_split','chinese_box','sphere_bounds','cylinder_bounds'):
                objective,basis = assembly_case() if name == 'assembly' else intake_case(name)
                planner,operations = (AssemblyRequirements(),AssemblyOperations()) if name == 'assembly' else (IntakeSemanticsRequirements(name), IntakeSemanticsOperations(name))
                async with build_agent_v2_workflow_worker(client, backend=get_execution_backend(),
                        durable_planner=planner,freecad_operations=operations):
                    await page.get_by_role('button',name='新建设计',exact=True).click()
                    await page.get_by_role('textbox',name='工程需求',exact=True).fill(objective)
                    await page.get_by_role('button',name='开始创建',exact=True).click()
                    intake = page.get_by_role('region',name='执行前需求确认',exact=True)
                    if name in {'cube', 'chinese_box', 'sphere_bounds', 'cylinder_bounds'}:
                        await expect(intake.get_by_role('textbox',name='关键尺寸依据',exact=True)).to_have_value(basis.dimensions)
                        await expect(intake.get_by_role('button',name='确认并执行',exact=True)).to_be_enabled()
                    else:
                        await intake.get_by_role('textbox',name='关键尺寸依据',exact=True).fill(basis.dimensions)
                    # Consent remains available with complete dimensions and
                    # does not erase them or silently opt in on submission.
                    consent = intake.get_by_role('checkbox',name='确认仅生成概念外形',exact=True)
                    await expect(consent).not_to_be_checked()
                    await consent.check()
                    await expect(intake.get_by_role('textbox',name='关键尺寸依据',exact=True)).to_have_value(basis.dimensions)
                    if name != 'assembly':
                        await consent.uncheck()
                    await intake.get_by_role('button',name='按概念外形继续' if name == 'assembly' else '确认并执行',exact=True).click()
                    card = page.locator('[data-testid=authoritative-task]:visible')
                    await expect(card).to_have_attribute('data-task-phase','candidate',timeout=300000)
                    workflow_id = await card.get_attribute('data-task-id')
                    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
                        saved = await conn.scalar(text('SELECT request_payload FROM workflow_runs WHERE id=:id'),{'id':UUID(workflow_id)})
                        report = await conn.scalar(text("SELECT evidence FROM agent_validation_evidence WHERE workflow_run_id=:id AND gate='geometry'"),{'id':UUID(workflow_id)})
                    assert saved['objective'] == objective
                    assert saved['operation_context']['requirement_basis']['dimensions'] == basis.dimensions
                    assert saved['operation_context']['requirement_basis']['concept_acknowledged'] is (name == 'assembly')
                    assert report['outcome'] == 'passed'
                    async with httpx.AsyncClient(base_url=os.environ['CAD_NATIVE_E2E_URL'],trust_env=False,
                            headers={'Authorization':'Bearer '+private['owner']['token']}) as api:
                        path = '/api/documents/'+saved['branch_id']
                        assert (await api.get(path)).raise_for_status().json()['head_revision_id'] == saved['expected_base_revision_id']
                        await card.locator('summary').filter(has_text='检查结果（').click()
                        await card.get_by_role('button',name='查看检查对象、版本与规则',exact=True).first.click()
                        if name in {'fdm','assembly'}:
                            await expect(card.get_by_test_id('engineering-verification-notice').filter(has_text='实际成品性能尚未')).to_have_count(1)
                            assert not report['acceptance_contract']['verification_limits']
                            if name == 'assembly':
                                await expect(card.get_by_test_id('engineering-verification-notice').filter(has_text='防护等级尚未通过实物')).to_have_count(1)
                                isolation=card.get_by_test_id('engineering-measurement').filter(has_text='mounting_holes_isolated')
                                await expect(isolation).to_contain_text('满足')
                                await expect(isolation).not_to_contain_text('1 mm')
                                await expect(card.get_by_test_id('engineering-measurement').filter(has_text='groove_depth')).to_contain_text('1.5 mm')
                        elif name == 'passage':
                            measured = card.get_by_test_id('engineering-measurement').filter(has_text='hole_through')
                            await expect(measured).to_contain_text('目标：贯通；实测：贯通')
                            criteria = {c['check_id']:c['kind'] for c in report['acceptance_contract']['checks']}
                            through = [e for e in report['acceptance']['evidence'] if criteria[e['check_id']] == 'hole_through']
                            assert len(through) == 1 and through[0]['measured'] == [1]
                        elif name.startswith('cube') or name in {'sphere_bounds', 'cylinder_bounds'}:
                            kind = name.split('_')[0]+'_shape'
                            await expect(card.get_by_test_id('engineering-measurement').filter(has_text=kind)).to_contain_text('目标：满足；实测：满足')
                            if name.startswith('cube'):
                                await expect(card.get_by_test_id('engineering-measurement').filter(has_text='cube_edge')).to_contain_text('25 mm' if name == 'cube_split' else '100 mm')
                        elif name == 'chinese_box':
                            for value in (100, 200, 300):
                                await expect(card.get_by_test_id('engineering-measurement').filter(has_text=f'{value} mm')).to_have_count(1)
                        else:
                            await expect(card.get_by_test_id('engineering-measurement').filter(has_text='40 mm')).to_have_count(1)
                        await card.get_by_role('button',name='审阅候选与参数变化',exact=True).click()
                        review = page.get_by_role('dialog',name='变更审查',exact=True)
                        await review.get_by_role('textbox',name='审查意见').fill('确认独立几何测量；打印性能、实物适配及视觉尚未验证。')
                        await review.get_by_role('button',name='应用修改',exact=True).click()
                        await expect(review.get_by_text('审查状态：committed',exact=True)).to_be_visible(timeout=30000)
                        await review.get_by_role('button',name='关闭',exact=True).last.click()
                        document = (await api.get(path)).raise_for_status().json()
                        assert document['head_revision_id'] != saved['expected_base_revision_id']
                        await page.reload()
                        await expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision',document['head_revision_id'],timeout=90000)
                        if name.startswith('cube') or name in {'sphere_bounds', 'cylinder_bounds'}:
                            card = page.locator('[data-testid=authoritative-task]:visible')
                            await card.locator('summary').filter(has_text='检查结果（').click()
                            await card.get_by_role('button',name='查看检查对象、版本与规则',exact=True).first.click()
                            await expect(card.get_by_test_id('engineering-measurement').filter(has_text=name.split('_')[0]+'_shape')).to_contain_text('目标：满足；实测：满足')
                        if name in {'fdm','assembly','passage'}:
                            card = page.locator('[data-testid=authoritative-task]:visible')
                            await card.locator('summary').filter(has_text='检查结果（').click()
                            await card.get_by_role('button',name='查看检查对象、版本与规则',exact=True).first.click()
                            if name == 'passage':
                                await expect(card.get_by_test_id('engineering-measurement').filter(has_text='hole_through')).to_contain_text('目标：贯通；实测：贯通')
                            else:
                                await expect(card.get_by_test_id('engineering-verification-notice').filter(has_text='实际成品性能尚未')).to_have_count(1)
                            if name == 'assembly':
                                await expect(card.get_by_test_id('engineering-measurement').filter(has_text='mounting_holes_isolated')).to_contain_text('满足')
                    assert operations.calls == 1 and len(planner.provider.requests) == 1
                    cases.append({'case':name,'workflow_id':workflow_id,'review_commit_reload':True})
                    await page.screenshot(path=str(out/(name+'-reloaded.png')))
            assert not errors,errors
            (out/'report.json').write_text(json.dumps({'passed':True,'cases':cases,'source_dimensions_preserved':True,
                'unverified_performance_visible_after_reload':True,'assembly_relationships_visible':True,
                'provider':'controlled-proposals','page_errors':errors},ensure_ascii=False,indent=2))
        except BaseException:
            await page.screenshot(path=str(out/'failure.png'))
            (out/'failure.txt').write_text(await page.locator('body').inner_text())
            raise
        finally:
            await page.context.tracing.stop(path=str(out/'trace.zip'))
            await browser.close()
    await close_database()
    print('INTAKE SEMANTICS BROWSER PASSED')


if __name__ == '__main__':
    asyncio.run(main())
