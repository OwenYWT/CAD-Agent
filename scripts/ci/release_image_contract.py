"""Real shipping-image upgrade/downgrade with immutable in-flight requests.

Called only inside deploy_contract's freshly-owned disposable Compose project.
There are no database rewrites, provider responses, or application source mounts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/tests/e2e'))
from native_parameter_contract import SEED
import importlib.util
_spec = importlib.util.spec_from_file_location('release_probe', ROOT / 'deploy/tencent/check-inflight.py')
_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_probe)


def main():
    private = Path(os.environ['CAD_CI_DEPLOY_PRIVATE'])
    compose = json.loads(os.environ['CAD_CI_DEPLOY_COMPOSE'])
    scope = os.environ['CAD_CI_SCOPE']
    if not scope.startswith('cad-ci-') or '-p' not in compose or compose[compose.index('-p') + 1] != scope:
        raise ValueError('only the owned disposable deployment is permitted')
    runtime = os.getenv('SANDBOX_COMMAND', 'docker')
    report = Path(os.environ['CAD_CI_REPORT_ROOT']) / 'image-transition'
    report.mkdir(exist_ok=False)
    images = {
        'current': {kind: os.environ[key] for kind, key in [('backend', 'BACKEND_IMAGE'), ('frontend', 'FRONTEND_IMAGE'), ('sandbox', 'SANDBOX_IMAGE')]},
        'previous': {kind: os.environ['CAD_CI_BASELINE_' + kind.upper() + '_IMAGE'] for kind in ('backend', 'frontend', 'sandbox')},
    }
    def run(args, **kwargs):
        return subprocess.run(args, check=True, text=True, **kwargs)
    ids = {label: {kind: run([runtime, 'image', 'inspect', value, '--format', '{{.Id}}'], capture_output=True).stdout.strip()
                   for kind, value in group.items()} for label, group in images.items()}
    assert ids['current']['backend'] != ids['previous']['backend'], 'two distinct release images required'
    people = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    api = httpx.Client(base_url=os.environ['CAD_NATIVE_E2E_URL'], timeout=30, trust_env=False,
                       headers={'Authorization': 'Bearer ' + people['owner']['token']})
    def call(method, path, **kwargs):
        value = api.request(method, path, **kwargs)
        value.raise_for_status()
        return value.json()
    def container(service):
        return run([*compose, 'ps', '-q', service], capture_output=True).stdout.strip()
    def wait_task(task, status):
        for _ in range(240):
            value = call('GET', '/api/tasks/' + task + '/snapshot')
            if value['status'] == status:
                return value
            if value['status'] in {'failed', 'timed_out', 'cancelled', 'succeeded'}:
                raise AssertionError((task, value['status'], value.get('error_code'), value.get('error_message')))
            time.sleep(.5)
        raise AssertionError('transition task did not reach ' + status)
    services = ('backend', 'workflow-worker', 'frontend', 'monitoring', 'edge')
    override_path = private / 'transition.yml'
    preflights = []
    def select(label):
        selected = images[label]
        result = _probe.probe_deployment(container('backend'), selected['backend'], runtime)
        preflights.append(result)
        (report / 'preflights.json').write_text(json.dumps(preflights, indent=2))
        if not result['compatible']:
            raise ValueError('Release switch blocked by in-flight compatibility preflight')
        # Quiesce workflow execution before the final check. Otherwise a signal
        # already acknowledged by Temporal could create a job after the read.
        worker = container('workflow-worker')
        run([*compose, 'stop', 'workflow-worker'])
        try:
            result = _probe.probe_deployment(container('backend'), selected['backend'], runtime)
            preflights.append({**result, 'worker_stopped_for_final_check': True})
            (report / 'preflights.json').write_text(json.dumps(preflights, indent=2))
            if not result['compatible']:
                raise ValueError('Release switch blocked by in-flight compatibility preflight')
        except BaseException:
            # Restart that exact container, preserving its image and config.
            # Compose defaults may already point at a different release.
            run([runtime, 'start', worker])
            raise
        override_path.write_text(yaml.safe_dump({'services': {
            key: {'image': selected['backend' if key in ('backend', 'workflow-worker', 'monitoring') else 'frontend']}
            for key in services}}))
        for name in ('backend.env', 'monitor.env'):
            target = private / name
            lines = target.read_text().splitlines()
            target.write_text('\n'.join('SANDBOX_IMAGE=' + selected['sandbox'] if line.startswith('SANDBOX_IMAGE=') else line for line in lines) + '\n')
        run([*compose, 'stop', 'backend', 'workflow-worker'])
        run([*compose, '-f', str(override_path), 'up', '-d', '--no-build', '--force-recreate', '--wait', *services])
        actual = json.loads(run([runtime, 'inspect', container('backend')], capture_output=True).stdout)[0]
        assert actual['Image'] == ids[label]['backend']
        assert all(not mount['Destination'].startswith(('/app/app', '/app/backend', '/workspace')) for mount in actual['Mounts'])
    # The seed is executed by the actual packaged backend and normal Temporal /
    # FreeCAD workers. This test never installs alternative worker operations.
    plan = {'operations': [
        {'op_id': 'sketch', 'action': 'sketch.create', 'args': {'name': 'Profile'}},
        {'op_id': 'circle', 'action': 'sketch.add_profile', 'args': {'sketch': 'Profile', 'geometry': {'kind': 'circle', 'radius_mm': 6, 'center': {'x': 0, 'y': 0}}}},
        {'op_id': 'pad', 'action': 'feature.pad', 'args': {'name': 'Pad', 'profile': 'Profile', 'length_mm': 9}},
        {'op_id': 'export', 'action': 'document.export', 'args': {'formats': ['fcstd', 'step', 'stl'], 'basename': 'transition'}},
    ]}
    select('previous')
    seeded = run([runtime, 'exec', '-i', container('backend'), 'python', '-', people['owner']['user']['id'], json.dumps(plan)], input=SEED, capture_output=True)
    (report / 'seed.log').write_text(seeded.stdout + seeded.stderr)
    fixture = json.loads(next(line.split('=', 1)[1] for line in seeded.stdout.splitlines() if line.startswith('NATIVE_PARAMETER_SEED=')))
    path = '/api/documents/' + fixture['document_id']
    def commit(change):
        for action in ('accept', 'commit'):
            call('POST', '/api/change-sets/' + change + '/' + action, json={'note': 'Image transition: native geometry is independently measured; visual provider unavailable.'})
    commit(fixture['change_set_id'])
    frozen = {}
    views = {}
    def persistent_revision(revision):
        reader = """import asyncio,json,sys
from sqlalchemy import text
from app.db import get_database_engine,close_database
async def main():
    async with get_database_engine().begin() as c:
        await c.execute(text('SET TRANSACTION READ ONLY'))
        revision=await c.scalar(text('SELECT to_jsonb(r) FROM project_revisions r WHERE id=:id'),{'id':sys.argv[1]})
        artifacts=(await c.execute(text('SELECT id::text,to_jsonb(a) FROM artifacts a WHERE revision_id=:id ORDER BY id'),{'id':sys.argv[1]})).all()
        request=await c.scalar(text('SELECT request_payload FROM workflow_runs WHERE id=:id'),{'id':revision['source_workflow_run_id']})
        print(json.dumps({'revision':revision,'artifacts':dict(artifacts),'request_payload':request}))
    await close_database()
asyncio.run(main())
"""
        result = run([runtime, 'exec', '-i', container('backend'), 'python', '-', revision], input=reader, capture_output=True)
        return json.loads(result.stdout)
    def remember(label, revision):
        frozen[revision] = persistent_revision(revision)
        views[(label, revision)] = call('GET', path + '/revisions/' + revision)
    def verify_history(label):
        for revision, old in frozen.items():
            now = persistent_revision(revision)
            assert now['revision'] == old['revision'] and now['request_payload'] == old['request_payload']
            assert all(now['artifacts'].get(key) == value for key, value in old['artifacts'].items())
            view = call('GET', path + '/revisions/' + revision)
            # Response schemas may gain provenance fields between releases.
            # Compare a view with the same reader; compare stored facts across
            # readers instead of mistaking new presentation metadata for writes.
            key = (label, revision)
            if key in views:
                exclude = {'head_revision_id', 'head_state_version'}
                assert {k: v for k, v in views[key].items() if k not in exclude} == {k: v for k, v in view.items() if k not in exclude}
            else:
                views[key] = view
        (report / 'immutable-revisions.json').write_text(json.dumps(frozen, ensure_ascii=False, indent=2))
        (report / 'revision-views.json').write_text(json.dumps({label + ':' + revision: value for (label, revision), value in views.items()}, ensure_ascii=False, indent=2))
    transitions = []
    incompatible_history_rejected = False
    initial = call('GET', path)
    remember('previous', initial['head_revision_id'])
    pending = call('POST', path + '/operations', json={'action': 'parameters.update',
        'expected_base_revision_id': initial['head_revision_id'], 'expected_state_version': initial['state_version'],
        'idempotency_key': str(uuid4()), 'modification': {'expected_state_sha256': initial['parameter_state_sha256'],
            'parameter_updates': [{'parameter_id': 'Pad.Length', 'value': 9.5}]}})
    running_task = pending['workflow_run_id']
    wait_task(running_task, 'waiting_confirmation')
    # Lock one actual queued/running job row after confirmation. This holds
    # the worker's final status write, without substituting its CAD execution.
    hold = """import asyncio,sys
from sqlalchemy import text
from app.db import get_database_engine,close_database
async def main():
    async with get_database_engine().begin() as c:
        print('JOB_OBSERVER_READY',flush=True)
        for _ in range(200):
            job=await c.scalar(text("SELECT id FROM model_jobs WHERE status IN ('queued','running') ORDER BY created_at LIMIT 1 FOR UPDATE"))
            if job is not None: break
            await asyncio.sleep(.05)
        else: raise AssertionError('No real active Model Job was observed')
        print('MODEL_JOB_LOCK_HELD',flush=True)
        value=await asyncio.wait_for(asyncio.to_thread(sys.stdin.readline),60)
        assert value.strip()=='release'
    await close_database()
asyncio.run(main())
"""
    with (report / 'model-job-lock.log').open('w') as log:
        barrier = subprocess.Popen([runtime, 'exec', '-i', container('backend'), 'python', '-c', hold],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True)
        try:
            import selectors
            with selectors.DefaultSelector() as poll:
                poll.register(barrier.stdout, selectors.EVENT_READ)
                assert poll.select(15), 'job observer did not start'
                assert barrier.stdout.readline().strip() == 'JOB_OBSERVER_READY'
                call('POST', '/api/tasks/' + running_task + '/confirmation', json={'accepted': True})
                assert poll.select(15), 'job observer did not acquire an active job'
                assert barrier.stdout.readline().strip() == 'MODEL_JOB_LOCK_HELD'
            before_switch = container('backend')
            try:
                select('current')
            except ValueError as error:
                assert 'in-flight compatibility' in str(error)
            else:
                raise AssertionError('Release changed during an active Model Job')
            assert any(item.get('reason') == 'active_model_jobs_require_drain' for item in preflights[-1]['blockers'])
            assert container('backend') == before_switch
            assert call('GET', path)['head_revision_id'] == initial['head_revision_id']
        finally:
            barrier.communicate(input='release\n', timeout=15)
            assert barrier.returncode == 0, 'model job barrier cleanup failed'
    finished = wait_task(running_task, 'succeeded')
    commit(finished['change_set']['id'])
    for source, target, length in [('previous', 'current', 10), ('current', 'previous', 11)]:
        before = call('GET', path)
        remember(source, before['head_revision_id'])
        submitted = call('POST', path + '/operations', json={'action': 'parameters.update',
            'expected_base_revision_id': before['head_revision_id'], 'expected_state_version': before['state_version'],
            'idempotency_key': str(uuid4()), 'modification': {'expected_state_sha256': before['parameter_state_sha256'],
                'parameter_updates': [{'parameter_id': 'Pad.Length', 'value': length}]}})
        task = submitted['workflow_run_id']
        pending = wait_task(task, 'waiting_confirmation')
        assert call('GET', path)['head_revision_id'] == before['head_revision_id']
        switched = False
        unchanged = container('backend')
        try:
            select(target)
            switched = True
            assert preflights[-1]['replayed_histories'], 'waiting task history was not replayed'
        except ValueError:
            assert target == 'previous'
            assert any(item['reason'] == 'workflow_history_incompatible' for item in preflights[-1]['blockers'])
            assert container('backend') == unchanged
            incompatible_history_rejected = True
        resumed = call('GET', '/api/tasks/' + task + '/snapshot')
        assert resumed['status'] == 'waiting_confirmation'
        assert resumed['request_payload'] == pending['request_payload']
        call('POST', '/api/tasks/' + task + '/confirmation', json={'accepted': True})
        done = wait_task(task, 'succeeded')
        assert call('GET', path)['head_revision_id'] == before['head_revision_id']
        commit(done['change_set']['id'])
        if not switched:
            # Finish and review on the current release before a safe rollback.
            # Never hand a history rejected by the old worker to that worker.
            select(target)
        saved = call('GET', path)
        assert saved['state_version'] == before['state_version'] + 1
        value = next(p['value'] for f in saved['features'] for p in f['parameters'] if p['id'] == 'Pad.Length')
        assert value == length
        exported = next(item for item in done['artifacts'] if item['artifact_kind'] == 'step')
        data = api.get(exported['download_url']).raise_for_status().content
        assert hashlib.sha256(data).hexdigest() == exported['sha256']
        measured_path = private / 'work' / 'measure'
        measured_path.mkdir(exist_ok=True)
        (measured_path / 'saved.step').write_bytes(data)
        kernel = "import json,Part; shape=Part.read('/measurements/saved.step'); assert shape.isValid() and len(shape.Solids)==1; print('TRANSITION_MEASUREMENT='+json.dumps({'volume':shape.Volume,'solids':len(shape.Solids)}))"
        measurement = run([runtime, 'run', '--rm', '--network', 'none', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,size=2g',
            '-v', str(measured_path) + ':/measurements:ro', '--entrypoint', '/opt/freecad/bin/FreeCADCmd',
            images[target]['sandbox'], '-c', kernel], capture_output=True)
        (report / (source + '-to-' + target + '-kernel.log')).write_text(measurement.stdout + measurement.stderr)
        assert 'Traceback (most recent call last)' not in measurement.stdout + measurement.stderr
        measured = json.loads(next(line.split('=', 1)[1] for line in measurement.stdout.splitlines() if line.startswith('TRANSITION_MEASUREMENT=')))
        import math
        assert measured['solids'] == 1 and abs(measured['volume'] - math.pi * 36 * length) < 1e-6
        transitions.append({'source': source, 'target': target, 'workflow_id': task,
                            'mode': 'continued_after_switch' if switched else 'drained_before_switch',
                            'candidate_review_required': True, 'revision': saved['head_revision_id'],
                            'step_sha256': exported['sha256'], 'actual_volume': measured['volume'], 'persisted_length': value})
        (report / 'completed-transitions.json').write_text(json.dumps(transitions, indent=2))
        verify_history(target)
    select('current')
    saved = call('GET', path)
    # A newly frozen DFM request must never be consumed by an older image which
    # ignores that snapshot. Prove the target rejects it before switching.
    run([*compose, 'stop', 'workflow-worker'])
    check = call('POST', '/api/analyze/' + transitions[-1]['workflow_id'], json={
        'asynchronous': True, 'source_revision_id': saved['head_revision_id'], 'process': 'CNC', 'description': 'Frozen transition check', 'idempotency_key': str(uuid4())})
    task = check['workflow_run_id']
    admitted = call('GET', '/api/tasks/' + task + '/snapshot')
    assert admitted['request_payload']['rule_configuration']
    unchanged_container = container('backend')
    try:
        select('previous')
    except ValueError as error:
        assert 'in-flight compatibility' in str(error)
    else:
        raise AssertionError('An incompatible release was allowed to consume a frozen check')
    assert container('backend') == unchanged_container
    probe = (ROOT / 'deploy/tencent/check-inflight.py').read_text()
    # The audit row omits server-derived execution identifiers; compatibility
    # applies to the actual immutable dispatch payload consumed by the worker.
    read_dispatch = '''import asyncio,json,sys
from sqlalchemy import text
from app.db import get_database_engine,close_database
async def main():
    async with get_database_engine().begin() as c:
        await c.execute(text("SET LOCAL ROLE cad_agent_dispatcher"))
        payload=await c.scalar(text("SELECT payload FROM workflow_dispatches WHERE workflow_id=:id"),{'id':sys.argv[1]})
        assert payload is not None
        print(json.dumps(payload))
    await close_database()
asyncio.run(main())
'''
    actual_dispatch = run([runtime, 'exec', '-i', container('backend'), 'python', '-', task], input=read_dispatch, capture_output=True)
    requests = [{'id': task, 'kind': admitted['kind'], 'request_payload': json.loads(actual_dispatch.stdout)}]
    def compatible(label):
        code = probe + '\n'
        # Pass source and private request via stdin, never a shell command or log.
        wrapper = 'import io,json,sys\nv=json.load(sys.stdin);sys.stdin=io.StringIO(json.dumps(v["requests"]));exec(compile(v["probe"],"check-inflight.py","exec"))'
        return subprocess.run([runtime, 'run', '--rm', '-i', '--network', 'none', '--read-only', '--entrypoint', 'python', images[label]['backend'], '-c', wrapper],
            input=json.dumps({'probe': code, 'requests': requests}), text=True, capture_output=True)
    rejected = compatible('previous')
    current = compatible('current')
    (report / 'rollback-preflight.json').write_text(rejected.stdout)
    (report / 'current-preflight.json').write_text(current.stdout)
    assert rejected.returncode == 2, rejected.stderr
    assert current.returncode == 0, current.stderr
    run([*compose, 'up', '-d', '--no-build', 'workflow-worker'])
    wait_task(task, 'succeeded')
    completed = call('GET', '/api/analyze/tasks/' + task)
    assert completed['analysis']['rule_configuration'] == admitted['request_payload']['rule_configuration']
    finished_check = call('GET', '/api/tasks/' + task + '/snapshot')
    report_artifact = next(item for item in finished_check['artifacts'] if item['artifact_kind'] == 'dfm_report')
    original_report = api.get(report_artifact['download_url']).raise_for_status().content
    assert hashlib.sha256(original_report).hexdigest() == completed['report_sha256'] == report_artifact['sha256']
    select('previous')
    legacy_snapshot = call('GET', '/api/tasks/' + task + '/snapshot')
    legacy_artifact = next(item for item in legacy_snapshot['artifacts'] if item['artifact_kind'] == 'dfm_report')
    assert legacy_artifact['sha256'] == report_artifact['sha256']
    assert api.get(legacy_artifact['download_url']).raise_for_status().content == original_report
    # The shipping edge only proxies /api; /openapi.json is the frontend SPA.
    # Inspect the actual running backend's registered GET routes, then exercise
    # the supported/absent result endpoint through the shipping HTTP entry.
    routes = run([runtime, 'exec', container('backend'), 'python', '-c',
        "import json; from app.main import app; print(json.dumps([r.path for r in app.routes if 'GET' in getattr(r,'methods',set())]))"], capture_output=True)
    legacy_route = any(re.fullmatch(r'/api/analyze/tasks/\{[^/]+\}', route) for route in json.loads(routes.stdout))
    if legacy_route:
        legacy = call('GET', '/api/analyze/tasks/' + task)
        (report / 'legacy-check.json').write_text(json.dumps(legacy, ensure_ascii=False, indent=2))
        # This baseline's response model predates configuration display.
        assert set(completed['analysis']) - set(legacy['analysis']) == {'rule_configuration'}
        assert legacy['analysis'] == {k: v for k, v in completed['analysis'].items() if k != 'rule_configuration'}
        assert legacy['report_sha256'] == completed['report_sha256']
    else:
        # Older releases have no durable-result route. The real task and signed
        # artifact download still have to preserve every byte and frozen input.
        assert api.get('/api/analyze/tasks/' + task).status_code == 404
    assert legacy_snapshot['request_payload'] == finished_check['request_payload']
    (report / 'current-check.json').write_text(json.dumps(completed, ensure_ascii=False, indent=2))
    assert call('GET', path)['head_revision_id'] == saved['head_revision_id']
    select('current')
    assert call('GET', '/api/analyze/tasks/' + task) == completed
    verify_history('current')
    (report / 'report.json').write_text(json.dumps({'passed': True, 'classification': 'SHIPPING_IMAGES_REAL_TEMPORAL_FREECAD',
        'images': ids, 'transitions': transitions, 'immutable_history': True,
        'active_model_job_switch_rejected': True,
        'history_replay_before_switch': True, 'incompatible_history_switch_rejected': incompatible_history_rejected,
        'worker_quiesced_before_final_preflight': True,
        'unsupported_inflight_rollback_rejected': True, 'frozen_check_drained_before_rollback': True,
        'legacy_result_route_available': legacy_route, 'raw_report_preserved_after_rollback': True,
        'complete_report_restored_after_upgrade': True,
        'restored_current_release': True, 'provider_calls': 0}, indent=2))
    print('SHIPPING IMAGE TRANSITION PASSED')


if __name__ == '__main__':
    main()
