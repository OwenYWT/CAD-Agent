"""Real UI -> WebSocket -> durable revised basis; no provider substitution.

The deterministic case cancels before executing a model. It proves admission and
immutability, not natural-language interpretation or new geometric acceptance.
"""
import json
import os
import signal
import time

from playwright.sync_api import expect


def verify_requirement_intake(page, api, fixture, out):
    path = '/api/documents/' + fixture['document_id']
    before = api.get(path).raise_for_status().json()
    revision_path = path + '/revisions/' + before['head_revision_id']
    original = api.get(revision_path).raise_for_status().json()
    inspector = page.locator('.ww-inspector-pane')
    if inspector.is_visible():
        inspector.get_by_role('button', name='返回 Agent', exact=True).click()
    clear = page.get_by_role('button', name='清除本次 AI 选择', exact=True)
    if clear.is_visible():
        clear.click()
    task_card = page.get_by_test_id('authoritative-task')
    previous_task = task_card.get_attribute('data-task-id') if task_card.count() else None
    prompt = '把板厚改为 10 mm，长度仍为 60 mm，宽度仍为 40 mm，贯穿孔直径仍为 12 mm。'
    page.get_by_role('textbox', name='询问 Agent', exact=True).fill(prompt)
    page.get_by_role('button', name='审查请求', exact=True).click()
    page.get_by_role('button', name='修改需求尺寸或依据', exact=True).click()
    card = page.get_by_role('region', name='执行前需求确认', exact=True)
    expect(card.get_by_role('textbox', name='建模目标', exact=True)).to_have_value(prompt)
    dimensions = '长度 60 mm；宽度 40 mm；厚度 10 mm；孔径 12 mm'
    card.get_by_role('textbox', name='关键尺寸依据', exact=True).fill(dimensions)
    # Editing a supplementary source must preserve the requested through hole,
    # operation and other conditions in the independently editable objective.
    expect(card.get_by_role('textbox', name='建模目标', exact=True)).to_have_value(prompt)
    expect(card.get_by_role('button', name='确认并执行', exact=True)).to_be_enabled()
    worker = int(os.environ['CAD_BROWSER_WORKER_PID'])
    os.kill(worker, signal.SIGSTOP)
    try:
        card.get_by_role('button', name='确认并执行', exact=True).click()
        expect(card).not_to_be_visible()
        expect(task_card).to_be_visible()
        if previous_task:
            expect(task_card).not_to_have_attribute('data-task-id', previous_task)
        expect(task_card).to_have_attribute('data-task-id', __import__('re').compile(r'^[0-9a-f-]{36}$'))
        task = task_card.get_attribute('data-task-id')
        snapshot = api.get('/api/tasks/' + task + '/snapshot').raise_for_status().json()
        accepted = snapshot['request_payload']
        assert accepted['operation'] == 'modify'
        assert accepted['objective'] == prompt
        assert accepted['expected_base_revision_id'] == before['head_revision_id']
        basis = accepted['operation_context']['requirement_basis']
        assert basis['dimensions'] == dimensions and basis['target'] == prompt
        assert basis['source_kind'] == 'user_specification'
        assert api.get(path).raise_for_status().json()['head_revision_id'] == before['head_revision_id']
        api.post('/api/tasks/' + task + '/cancel', json={}).raise_for_status()
    finally:
        os.kill(worker, signal.SIGCONT)
    for _ in range(120):
        snapshot = api.get('/api/tasks/' + task + '/snapshot').raise_for_status().json()
        if snapshot['status'] == 'cancelled':
            break
        assert snapshot['status'] not in {'failed', 'succeeded', 'timed_out'}, snapshot.get('error_message')
        time.sleep(.25)
    else:
        raise AssertionError('Cancelled intake did not reach its durable terminal state')
    assert api.get(revision_path).raise_for_status().json() == original
    assert snapshot['request_payload'] == accepted
    (out / 'requirements-intake.json').write_text(json.dumps({'passed': True,
        'classification': 'REAL_BROWSER_WEBSOCKET_DURABLE_ADMISSION',
        'workflow_id': task, 'new_confirmed_dimensions': dimensions,
        'original_revision_immutable': True, 'saved_head_unchanged': True,
        'cancelled_before_provider_execution': True,
        'natural_language_geometry_evaluated': False}, ensure_ascii=False, indent=2))


def verify_initial_geometry_basis(page, api, out):
    """Preserve submitted feature context through the real UI and durable API.

    Cancellation before provider execution makes this an admission test, not
    evidence that every feature in these requests has been modeled or verified.
    """
    rows = []
    for prompt, edited_dimensions in [
        ('创建顶部开口盒体，长 80 mm、宽 55 mm、高 32 mm；内部腔体长 74 mm、宽 49 mm、深 29 mm。', None),
        ('创建长 80 mm、宽 55 mm、高 32 mm 的盒体。不包含上盖、装配结构、配合间隙和螺纹。', None),
        ('单位：mm\n外壳外尺寸：长 80，宽 55，高 32\n壁厚：3', None),
        ('创建长80mm、宽40mm、厚8mm的板，保留一个直径6mm的居中通孔。',
         '长度80mm；宽度40mm；厚度8mm；孔径6mm'),
    ]:
        page.get_by_role('button', name='新建设计', exact=True).click()
        page.get_by_role('textbox', name='工程需求', exact=True).fill(prompt)
        page.get_by_role('button', name='开始创建', exact=True).click()
        card = page.get_by_role('region', name='执行前需求确认', exact=True)
        expect(card.get_by_role('textbox', name='关键尺寸依据', exact=True)).to_have_value(prompt)
        if edited_dimensions:
            card.get_by_role('textbox', name='关键尺寸依据', exact=True).fill(edited_dimensions)
        expect(card.get_by_role('textbox', name='建模目标', exact=True)).to_have_value(prompt)
        if '\n' in prompt:
            # Exercise the target editor too: a single-line input normalizes
            # away line breaks and can join a unit declaration to its clause.
            card.get_by_role('textbox', name='建模目标', exact=True).fill(prompt + '\n')
            card.get_by_role('textbox', name='建模目标', exact=True).fill(prompt)
            expect(card.get_by_role('textbox', name='建模目标', exact=True)).to_have_value(prompt)
        expect(card.get_by_role('button', name='确认并执行', exact=True)).to_be_enabled()
        assert '适配未验证' not in card.inner_text()
        worker = int(os.environ['CAD_BROWSER_WORKER_PID'])
        os.kill(worker, signal.SIGSTOP)
        try:
            card.get_by_role('button', name='确认并执行', exact=True).click()
            task_card = page.get_by_test_id('authoritative-task')
            expect(task_card).to_have_attribute('data-task-id', __import__('re').compile(r'^[0-9a-f-]{36}$'))
            task = task_card.get_attribute('data-task-id')
            snapshot = api.get('/api/tasks/' + task + '/snapshot').raise_for_status().json()
            accepted = snapshot['request_payload']
            basis = accepted['operation_context']['requirement_basis']
            assert accepted['objective'] == prompt and basis['target'] == prompt
            assert basis['dimensions'] == (edited_dimensions or prompt)
            assert basis['design_scope'] == 'geometry' and basis['source_kind'] == 'user_specification'
            api.post('/api/tasks/' + task + '/cancel', json={}).raise_for_status()
        finally:
            os.kill(worker, signal.SIGCONT)
        for _ in range(120):
            snapshot = api.get('/api/tasks/' + task + '/snapshot').raise_for_status().json()
            if snapshot['status'] == 'cancelled':
                break
            assert snapshot['status'] not in {'failed', 'succeeded', 'timed_out'}, snapshot.get('error_message')
            time.sleep(.25)
        else:
            raise AssertionError('New design intake did not reach cancelled state')
        assert snapshot['request_payload'] == accepted
        rows.append({'workflow_id': task, 'dimensions': basis['dimensions'],
                     'cancelled_before_provider_execution': True})
    (out / 'initial-geometry-intake.json').write_text(json.dumps({'passed': True,
        'classification': 'REAL_BROWSER_WEBSOCKET_DURABLE_ADMISSION', 'cases': rows,
        'natural_language_geometry_evaluated': False}, ensure_ascii=False, indent=2))
