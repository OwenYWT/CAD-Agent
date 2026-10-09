"""Public parameter edit, browser history restore, candidate commit and reopen."""
import json
import time
from uuid import uuid4

from playwright.sync_api import expect


def verify_restore(page, api, fixture, out):
    path = '/api/documents/' + fixture['document_id']
    before = api.get(path).raise_for_status().json()
    original = before['head_revision_id']
    original_evidence = api.get(path + '/revisions/' + original).raise_for_status().json()
    parameter = fixture['long_name'] + '.Length'
    values = {p['id']: p['value'] for feature in before['features'] for p in feature['parameters']}
    assert values[parameter] == 9

    def terminal(task):
        confirmed = False
        for _ in range(240):
            snapshot = api.get('/api/tasks/' + task + '/snapshot').raise_for_status().json()
            if snapshot['status'] == 'waiting_confirmation' and not confirmed:
                api.post('/api/tasks/' + task + '/confirmation', json={'accepted': True}).raise_for_status()
                confirmed = True
            if snapshot['status'] in {'succeeded', 'failed', 'cancelled', 'timed_out'}:
                assert snapshot['status'] == 'succeeded', snapshot.get('error_message')
                return snapshot
            time.sleep(.5)
        raise AssertionError('Native workflow did not finish: ' + task)

    edited = api.post(path + '/operations', json={'action': 'parameters.update',
        'expected_base_revision_id': original, 'expected_state_version': before['state_version'],
        'idempotency_key': str(uuid4()), 'modification': {'expected_state_sha256': before['parameter_state_sha256'],
            'parameter_updates': [{'parameter_id': parameter, 'value': 10}]}}).raise_for_status().json()
    candidate = terminal(edited['workflow_run_id'])
    for action in ('accept', 'commit'):
        api.post('/api/change-sets/' + candidate['change_set']['id'] + '/' + action,
            json={'note': 'Native restore fixture; exact geometry checked after restore.'}).raise_for_status()
    middle = api.get(path).raise_for_status().json()
    assert middle['head_revision_id'] != original
    middle_evidence = api.get(path + '/revisions/' + middle['head_revision_id']).raise_for_status().json()
    page.reload()
    expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision', middle['head_revision_id'], timeout=90000)
    page.get_by_role('button', name='版本', exact=True).click()
    row = page.locator(f'[data-revision-id="{original}"]')
    row.get_by_role('button', name='从此版本生成恢复候选', exact=True).click()
    if page.locator('.ww-inspector-pane').is_visible():
        page.locator('.ww-inspector-pane').get_by_role('button', name='返回 Agent', exact=True).click()
    confirmation = page.get_by_role('group', name='服务端执行计划确认', exact=True)
    expect(confirmation).to_be_visible(timeout=90000)
    card = page.get_by_test_id('authoritative-task')
    task = card.get_attribute('data-task-id')
    accepted = api.get('/api/tasks/' + task + '/snapshot').raise_for_status().json()
    assert accepted['request_payload']['revision_restore']['source_revision_id'] == original
    assert accepted['request_payload']['expected_base_revision_id'] == middle['head_revision_id']
    confirmation.get_by_role('button', name='确认并继续', exact=True).click()
    expect(card).to_have_attribute('data-task-phase', 'candidate', timeout=120000)
    assert api.get(path).raise_for_status().json()['head_revision_id'] == middle['head_revision_id']
    card.get_by_role('button', name='审阅候选与参数变化', exact=True).click()
    review = page.get_by_role('dialog', name='变更审查', exact=True)
    review.get_by_role('textbox', name='审查意见').fill('已核对恢复来源和参数；视觉模型未配置，外观与制造适配不计为通过。恢复后的原生体积将独立重开复测。')
    review.get_by_role('button', name='应用修改', exact=True).click()
    expect(review.get_by_text('审查状态：committed', exact=True)).to_be_visible(timeout=30000)
    review.get_by_role('button', name='关闭', exact=True).last.click()
    restored = api.get(path).raise_for_status().json()
    assert restored['head_revision_id'] not in {original, middle['head_revision_id']}
    assert restored['state_version'] == before['state_version'] + 2
    assert {p['id']: p['value'] for f in restored['features'] for p in f['parameters']} == values
    page.reload()
    expect(page.get_by_test_id('document-scene')).to_have_attribute('data-revision', restored['head_revision_id'], timeout=90000)
    measurement = api.post(path + '/engineering', json={
        'expected_revision_id': restored['head_revision_id'], 'expected_state_version': restored['state_version'],
        'idempotency_key': str(uuid4()), 'task': {'kind': 'native_measure', 'measurement': 'volume', 'component_name': 'Body', 'selectors': []}}).raise_for_status().json()
    terminal(measurement['workflow_run_id'])
    result = api.get(path + '/engineering/' + measurement['workflow_run_id']).raise_for_status().json()
    prior_measurement = json.loads((out / 'native-volume.json').read_text())
    assert abs(result['report']['value'] - prior_measurement['report']['value']) < 1e-6
    for revision, frozen in [(original, original_evidence), (middle['head_revision_id'], middle_evidence)]:
        reopened = api.get(path + '/revisions/' + revision).raise_for_status().json()
        # The historical view explicitly includes the *current* branch pointer.
        # Its model, requirements, files and review evidence remain immutable.
        assert reopened['head_revision_id'] == restored['head_revision_id']
        assert reopened['head_state_version'] == restored['state_version']
        mutable_head = {'head_revision_id', 'head_state_version'}
        assert {k: v for k, v in reopened.items() if k not in mutable_head} == {
            k: v for k, v in frozen.items() if k not in mutable_head}
    assert api.post(path + '/releases', json={'release_name': 'Reject historical evidence',
        'expected_revision_id': restored['head_revision_id'], 'expected_state_version': restored['state_version'],
        'idempotency_key': str(uuid4()), 'engineering_workflow_ids': [prior_measurement['workflow_run_id']]}).status_code == 422
    page.screenshot(path=str(out / 'restored-reopened.png'))
    (out / 'restore.json').write_text(json.dumps({'passed': True, 'classification': 'REAL_BROWSER_API_TEMPORAL_FREECAD',
        'source_revision': original, 'intermediate_revision': middle['head_revision_id'], 'restored_revision': restored['head_revision_id'],
        'new_candidate_before_commit': True, 'original_revisions_immutable': True, 'old_release_evidence_rejected': True,
        'reopened_volume': result['report']['value'], 'restored_parameters': values}, indent=2))
