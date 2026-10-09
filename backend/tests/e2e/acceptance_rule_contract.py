"""Owner rules -> durable admission -> real FreeCAD -> frozen report regressions."""
import json
import math
import os
from pathlib import Path
import signal
import time
from uuid import uuid4

import httpx
from app.freecad.operation_compiler import compile_common_generation
from native_parameter_contract import SEED
from runtime_fixture import run_native_seed


def main():
    out = Path(os.environ['CAD_ACCEPTANCE_FOLLOWUP_REPORT'])
    out.mkdir(parents=True, exist_ok=False)
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    title = 'Acceptance followup ' + uuid4().hex[:8]
    plan = compile_common_generation({'part_type': 'plate', 'dimensions': {'length': 60, 'width': 40, 'thickness': 9},
        'features': ['through_hole:diameter=12,position=centered'], 'constraints': []}, output_formats=('step', 'stl'))
    assert plan is not None
    operations = plan.model_dump(mode='json')
    long_name = 'Pad_' + 'Readable_feature_name_' * 3
    old_name = next(op['args']['name'] for op in operations['operations'] if op['action'] == 'feature.pad')
    def rename(value):
        if isinstance(value, dict): return {key: rename(item) for key, item in value.items()}
        if isinstance(value, list): return [rename(item) for item in value]
        return long_name if value == old_name else value
    operations = rename(operations)
    seed_source = SEED.replace("title='Native parameter contract'", 'title=sys.argv[3]')
    seed = run_native_seed(seed_source, [private['owner']['user']['id'], json.dumps(operations), title])
    (out/'seed.log').write_text(seed.stdout + seed.stderr)
    assert seed.returncode == 0, 'Real native fixture failed; see seed.log'
    fixture = json.loads(next(line.split('=', 1)[1] for line in seed.stdout.splitlines() if line.startswith('NATIVE_PARAMETER_SEED=')))
    api_url = os.environ['CAD_NATIVE_E2E_URL']
    with httpx.Client(base_url=api_url, timeout=120, trust_env=False,
        headers={'Authorization': 'Bearer ' + private['owner']['token']}) as api:
        def call(method, path, **kwargs):
            response = api.request(method, path, **kwargs); response.raise_for_status(); return response.json()
        def terminal(workflow):
            for _ in range(240):
                value = call('GET', f'/api/tasks/{workflow}/snapshot')
                if value['status'] in {'succeeded', 'failed', 'cancelled', 'timed_out'}:
                    assert value['status'] == 'succeeded', value.get('error_message')
                    return value
                time.sleep(.5)
            raise AssertionError('Check did not complete: ' + workflow)
        for action in ('accept', 'commit'):
            call('POST', f'/api/change-sets/{fixture["change_set_id"]}/{action}', json={'note': 'Native geometry and unsupported DFM metrics are independently checked.'})
        doc = call('GET', '/api/documents/' + fixture['document_id'])
        source = '/api/analyze/' + fixture['workflow_id']
        body = {'asynchronous': True, 'source_revision_id': doc['head_revision_id'], 'process': 'CNC', 'description': title}
        rules = call('GET', '/api/dfm/rules/CNC')
        original = next(rule for rule in rules if rule['id'] == 'cnc_max_size')
        worker = int(os.environ['CAD_BROWSER_WORKER_PID'])
        # This is the owned worker PID exported by browser_contract.sh. Pausing
        # it proves the snapshot was captured at admission, before execution.
        try:
            call('PUT', '/api/dfm/rules/cnc_max_size', json={'threshold_max': 1, 'enabled': True})
            os.kill(worker, signal.SIGSTOP)
            try:
                explicit = {**body, 'idempotency_key': str(uuid4())}
                first = call('POST', source, json=explicit)
                admitted = call('GET', '/api/tasks/' + first['workflow_run_id'] + '/snapshot')
                frozen = admitted['request_payload']['rule_configuration']
                assert next(rule for rule in frozen['rules'] if rule['id'] == 'cnc_max_size')['threshold_max'] == 1
                call('PUT', '/api/dfm/rules/cnc_max_size', json={'enabled': False, 'threshold_max': 1000})
                assert call('POST', source, json=explicit)['workflow_run_id'] == first['workflow_run_id']
                assert api.post(source, json={**explicit, 'description': title + ' changed'}).status_code == 409
            finally:
                os.kill(worker, signal.SIGCONT)
            terminal(first['workflow_run_id'])
            report = call('GET', '/api/analyze/tasks/' + first['workflow_run_id'])
            analysis = report['analysis']
            assert analysis['rule_configuration'] == frozen
            actual = {rule['rule_id']: rule for rule in analysis['evaluated_rules']}
            assert actual['cnc_max_size']['status'] == 'violated'
            assert actual['cnc_max_size']['actual_value'] > 59 and actual['cnc_max_size']['threshold_max'] == 1
            # A 12 mm diameter is a length, never the 9/12 depth ratio.
            assert actual['cnc_max_depth_ratio']['status'] == 'indeterminate'
            assert actual['cnc_max_depth_ratio']['actual_value'] is None
            second = call('POST', source, json=body)
            terminal(second['workflow_run_id'])
            disabled = call('GET', '/api/analyze/tasks/' + second['workflow_run_id'])
            item = next(rule for rule in disabled['analysis']['evaluated_rules'] if rule['rule_id'] == 'cnc_max_size')
            assert item['status'] == 'disabled' and item['actual_value'] is None
            assert disabled['analysis']['rule_configuration']['sha256'] != frozen['sha256']
            assert call('POST', source, json=body)['workflow_run_id'] == second['workflow_run_id']
            call('PUT', '/api/dfm/rules/cnc_max_size', json={'enabled': True, 'threshold_max': 1})
            third = call('POST', source, json=body)
            assert third['workflow_run_id'] not in {first['workflow_run_id'], second['workflow_run_id']}
            terminal(third['workflow_run_id'])
            with httpx.Client(base_url=api_url, trust_env=False, headers={'Authorization': 'Bearer ' + private['editor']['token']}) as other:
                other_rules = other.get('/api/dfm/rules/CNC'); other_rules.raise_for_status()
                assert next(rule for rule in other_rules.json() if rule['id'] == 'cnc_max_size')['threshold_max'] == original['threshold_max']
                assert other.get('/api/analyze/tasks/' + first['workflow_run_id']).status_code in {403, 404}
            measurement = call('POST', '/api/documents/' + fixture['document_id'] + '/engineering', json={
                'expected_revision_id': doc['head_revision_id'], 'expected_state_version': doc['state_version'],
                'idempotency_key': str(uuid4()), 'task': {'kind': 'native_measure', 'measurement': 'volume', 'component_name': 'Body', 'selectors': []}})
            terminal(measurement['workflow_run_id'])
            evidence = call('GET', f'/api/documents/{fixture["document_id"]}/engineering/{measurement["workflow_run_id"]}')
            assert abs(evidence['report']['value'] - (60*40*9 - math.pi*6*6*9)) < 1e-6
            assert call('GET', '/api/documents/' + fixture['document_id'])['head_revision_id'] == doc['head_revision_id']
            for name, value in [('enabled-report', report), ('disabled-report', disabled), ('native-volume', evidence)]:
                (out/(name+'.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2))
            (out/'fixture.json').write_text(json.dumps({**fixture, 'title': title, 'long_name': long_name, 'document': doc}, ensure_ascii=False, indent=2))
            (out/'rules.json').write_text(json.dumps({'passed': True, 'classification': 'REAL_API_TEMPORAL_FREECAD',
                'cases': ['owner_rules_applied', 'disabled_rules_not_executed', 'configuration_frozen_at_admission',
                'transport_retry_reuses_original_configuration', 'changed_configuration_new_implicit_request',
                'tenant_rules_isolated', 'depth_ratio_indeterminate_instead_of_diameter', 'native_volume_and_saved_head']}, indent=2))
        finally:
            call('PUT', '/api/dfm/rules/cnc_max_size', json={key: original[key] for key in ('threshold_max', 'enabled')})
    print('ACCEPTANCE RULE CONTRACT PASSED')


if __name__ == '__main__': main()
