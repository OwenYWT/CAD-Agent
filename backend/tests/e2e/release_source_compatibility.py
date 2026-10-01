"""Cross-release DB protocol drill, not a cloud/image deployment acceptance.

Run this file in separate processes with old/current PYTHONPATH. The operation
returns controlled test data; real PostgreSQL roles, leases and fencing execute.
An expired lease models a dead worker, not a new elapsed-time execution limit.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4


async def phase(action, state_path):
    from sqlalchemy import text
    from app.config import settings
    from app.db import close_database, tenant_transaction
    from app.domain.identity import user_principal
    from app.repositories.identity import reconcile_principal
    from app.repositories.projects import create_project
    from app.services.run_state import create_workflow
    from app.services import model_jobs as jobs

    settings.database_url = os.environ['CAD_RELEASE_TEST_DATABASE_URL']
    settings.durable_control_plane_enabled = True
    try:
        if action == 'seed':
            owner = user_principal('release-drill-' + str(uuid4()))
            await reconcile_principal(owner)
            project = uuid4()
            async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
                await create_project(conn, project_id=project, tenant_id=owner.tenant_id,
                    creator_principal_id=owner.principal_id, name='release drill', slug=str(project))
                run = await create_workflow(conn, tenant_id=owner.tenant_id, project_id=project,
                    requested_by_principal_id=owner.principal_id, kind='mcad.agent.v2.generate',
                    idempotency_key=str(uuid4()), request_payload={})
            request = {'job_id': str(uuid4()), 'operation': 'agent_v2.requirements', 'payload': {
                'tenant_id': str(owner.tenant_id), 'principal_id': str(owner.principal_id),
                'workflow_run_id': str(run.workflow_id), 'measurement': 1.2345678901234567e20}}
            assert (await jobs.submit(request))['status'] == 'queued'
            job = await jobs.claim()
            assert str(job['id']) == request['job_id']
            async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
                await conn.execute(text("UPDATE model_jobs SET lease_until=now()-interval '1 second' WHERE id=:id"), job)
            # Only public identifiers/generation are needed by the stale writer.
            retained = {k: str(job[k]) if isinstance(job[k], UUID) else job[k]
                        for k in ('id', 'tenant_id', 'principal_id', 'generation')}
            state_path.write_text(json.dumps({'request': request, 'old_job': retained}))
        else:
            state = json.loads(state_path.read_text())
            request = state['request']
            old_job = {k: UUID(v) if k in {'id', 'tenant_id', 'principal_id'} else v
                       for k, v in state['old_job'].items()}
            if action == 'recover':
                job = await jobs.claim()
                assert str(job['id']) == request['job_id']
                assert job['generation'] == old_job['generation'] + 1
                async def controlled_operation(payload):
                    return {'measurement': payload['measurement'], 'generation': job['generation']}
                await jobs.execute(job, {request['operation']: controlled_operation})
            elif action == 'stale':
                await jobs.finish(old_job, status='succeeded', result={'stale_write': True})
            result = await jobs.read(request)
            assert result['status'] == 'succeeded'
            assert result['result'] == {'measurement': request['payload']['measurement'],
                                        'generation': old_job['generation'] + 1}
    finally:
        await close_database()


def main(old, current, output):
    if not os.environ.get('CAD_RELEASE_TEST_DATABASE_URL'):
        raise SystemExit('dedicated migrated release drill database required')
    output.mkdir(parents=True, exist_ok=False)
    sources = {}
    for label, source in [('old', old), ('current', current)]:
        digest = hashlib.sha256()
        for path in sorted((source / 'backend/app').rglob('*.py')):
            digest.update(path.relative_to(source).as_posix().encode() + b'\0' + path.read_bytes())
        sources[label] = {
            'head': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
            'app_source_sha256': digest.hexdigest(),
            'dirty': bool(subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'], text=True)),
        }
    script = str(Path(__file__).resolve())
    for direction, first, second in [('upgrade', old, current), ('rollback', current, old)]:
        state = output / (direction + '.json')
        for action, source in [('seed', first), ('recover', second), ('stale', first), ('verify', second)]:
            env = {**os.environ, 'PYTHONPATH': str(source / 'backend'), 'APP_ENVIRONMENT': 'test'}
            subprocess.run([sys.executable, script, '--phase', action, '--state', str(state)],
                           cwd=source / 'backend', env=env, check=True)
    for label, source in [('old', old), ('current', current)]:
        env = {**os.environ, 'PYTHONPATH': str(source / 'backend'), 'APP_ENVIRONMENT': 'test',
               'CAD_MONITOR_TEST_DATABASE_URL': os.environ['CAD_RELEASE_TEST_DATABASE_URL']}
        with (output / (label + '-monitor.log')).open('w') as log:
            subprocess.run([sys.executable, '-m', 'pytest', 'tests/postgres/test_monitoring.py', '-q',
                            '--junitxml=' + str(output / (label + '-monitor.xml'))],
                           cwd=source / 'backend', env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    (output / 'summary.json').write_text(json.dumps({'passed': True, 'sources': sources,
        'upgrade_fencing': True, 'rollback_fencing': True, 'old_new_monitor_roles': True,
        'scope': 'source processes and database; excludes deployment images/cloud'}, indent=2))
    print('CROSS-RELEASE SOURCE/DB DRILL PASSED')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--old', type=Path)
    parser.add_argument('--current', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--phase', choices=['seed', 'recover', 'stale', 'verify'])
    parser.add_argument('--state', type=Path)
    args = parser.parse_args()
    if args.phase:
        asyncio.run(phase(args.phase, args.state))
    else:
        main(args.old.resolve(), args.current.resolve(), args.output.resolve())
