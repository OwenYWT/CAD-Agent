"""Restore a quiesced, isolated MCAD deployment into new resources.

This is an acceptance drill, not a production disaster-recovery command. It
refuses non-local APIs and resources outside the cad-coedit test namespace.
PostgreSQL, current object versions (including immutable historical revisions),
and its PostgreSQL authentication tables are backed up together. Temporal
history compatibility is checked separately by native_temporal_replay.py.

Run on the host with CAD_NATIVE_E2E_{URL,PRIVATE,HOST_ENV,ENV},
CAD_NATIVE_E2E_API, CAD_NATIVE_E2E_WORKER and CAD_JOINT_RESTORE_DIR. Object I/O
runs inside the container VM so signed S3 requests use the server's clock.
No existing database, bucket, object or container is removed or overwritten.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import httpx


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def save(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    path.chmod(0o600)


def with_database(url: str, database: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path='/' + database))


async def database_facts(url: str) -> dict:
    connection = await asyncpg.connect(url.replace('postgresql+asyncpg:', 'postgresql:'))
    try:
        tables = await connection.fetch("""SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind='r' ORDER BY c.relname""")
        result = {}
        async with connection.transaction(isolation='repeatable_read', readonly=True):
            for table in tables:
                name = table['relname']
                assert re.fullmatch(r'[a-z_][a-z0-9_]*', name)
                rows = await connection.fetch(f'SELECT to_jsonb(t)::text AS row FROM public."{name}" t ORDER BY 1')
                encoded = '\n'.join(row['row'] for row in rows).encode()
                result[name] = {'rows':len(rows), 'sha256':hashlib.sha256(encoded).hexdigest(),
                    'rls':table['relrowsecurity'], 'force_rls':table['relforcerowsecurity']}
            catalogs = {}
            for name, query in {
                'columns':"SELECT table_name,column_name,ordinal_position,data_type,udt_name,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position",
                'policies':"SELECT tablename,policyname,permissive,roles,cmd,qual,with_check FROM pg_policies WHERE schemaname='public' ORDER BY tablename,policyname",
                'constraints':"SELECT r.relname,c.conname,pg_get_constraintdef(c.oid) AS definition FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public' ORDER BY r.relname,c.conname",
                'indexes':"SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY tablename,indexname",
            }.items():
                rows = [dict(row) for row in await connection.fetch(query)]
                catalogs[name] = hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
            sequences = {}
            for row in await connection.fetch("SELECT sequencename FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename"):
                name = row['sequencename']
                assert re.fullmatch(r'[a-z_][a-z0-9_]*', name)
                sequences[name] = dict(await connection.fetchrow(f'SELECT last_value,is_called FROM public."{name}"'))
        return {'tables':result, 'catalogs':catalogs, 'sequences':sequences}
    finally:
        await connection.close()


def objects(mode: str, out: Path, target_bucket: str | None) -> None:
    from app.config import settings
    from app.object_store import get_object_store_client

    source = settings.object_store_bucket
    assert source.startswith('cad-coedit-live-')
    s3 = get_object_store_client()
    if mode == 'backup':
        assert s3.get_bucket_versioning(Bucket=source)['Status'] == 'Enabled'
        folder = out / 'objects'
        folder.mkdir(mode=0o700)
        manifest = []
        for page in s3.get_paginator('list_objects_v2').paginate(Bucket=source):
            for item in page.get('Contents', []):
                response = s3.get_object(Bucket=source, Key=item['Key'])
                path = folder / f'{len(manifest):08d}.bin'
                try:
                    with path.open('xb') as handle:
                        shutil.copyfileobj(response['Body'], handle)
                finally:
                    response['Body'].close()
                path.chmod(0o600)
                assert path.stat().st_size == item['Size']
                manifest.append({'key':item['Key'], 'path':path.name, 'size':path.stat().st_size,
                    'sha256':digest(path), 'content_type':response['ContentType'],
                    'metadata':response.get('Metadata', {}), 'source_version_id':response.get('VersionId')})
        assert manifest, 'empty object storage cannot establish historical recovery'
        save(out / 'object-manifest.json', manifest)
        print(json.dumps({'objects_backed_up':len(manifest), 'bytes':sum(x['size'] for x in manifest)}), flush=True)
        return
    assert target_bucket and target_bucket.startswith('cad-coedit-restore-') and target_bucket != source
    assert target_bucket not in [item['Name'] for item in s3.list_buckets()['Buckets']], 'target must be new'
    s3.create_bucket(Bucket=target_bucket)
    s3.put_bucket_versioning(Bucket=target_bucket, VersioningConfiguration={'Status':'Enabled'})
    manifest = json.loads((out / 'object-manifest.json').read_text())
    for item in manifest:
        path = out / 'objects' / item['path']
        assert digest(path) == item['sha256']
        with path.open('rb') as handle:
            s3.upload_fileobj(handle, target_bucket, item['key'], ExtraArgs={
                'ContentType':item['content_type'], 'Metadata':item['metadata']})
        response = s3.get_object(Bucket=target_bucket, Key=item['key'])
        measured = hashlib.sha256()
        length = 0
        try:
            for chunk in iter(lambda: response['Body'].read(8 * 1024 * 1024), b''):
                measured.update(chunk)
                length += len(chunk)
        finally:
            response['Body'].close()
        assert measured.hexdigest() == item['sha256'] and length == item['size']
        assert response['Metadata'] == item['metadata']
    save(out / 'object-restore.json', {'count':len(manifest), 'all_bytes_and_metadata_verified':True,
        'bucket_versioning':s3.get_bucket_versioning(Bucket=target_bucket)['Status']})


def http_facts(base: str, private: dict) -> dict:
    with httpx.Client(base_url=base, timeout=60, headers={'Authorization':'Bearer ' + private['owner']['token']}) as client:
        path = '/api/documents/' + private['document_id']
        response = client.get(path)
        response.raise_for_status()
        current = response.json()
        response = client.get('/api/history/panels/' + private['panel_id'] + '/snapshots')
        response.raise_for_status()
        history = response.json()
        result = {'head_revision_id':current['head_revision_id'], 'state_version':current['state_version'],
            'parameter_state_sha256':current['parameter_state_sha256'], 'revisions':{}}
        # Verify the head and every retained native historical revision through
        # authenticated application routes, not only administrative S3 access.
        for record in history:
            response = client.get(path + '/revisions/' + record['id'])
            response.raise_for_status()
            view = response.json()
            files = view['snapshot'].get('files', {})
            if not files.get('fcstd'):
                continue
            hashes = {}
            for kind in ('fcstd', 'step', 'state'):
                if kind not in files:
                    continue
                downloaded = client.get(files[kind])
                downloaded.raise_for_status()
                hashes[kind] = hashlib.sha256(downloaded.content).hexdigest()
            result['revisions'][record['id']] = hashes
        assert len(result['revisions']) >= 2 and result['head_revision_id'] in result['revisions']
        return result


def main() -> None:
    os.umask(0o077)
    out = Path(os.environ['CAD_JOINT_RESTORE_DIR']).resolve()
    out.mkdir(parents=True, mode=0o700, exist_ok=False)
    root = Path(__file__).resolve().parents[3]
    base = os.environ['CAD_NATIVE_E2E_URL']
    assert urlsplit(base).hostname in {'localhost','127.0.0.1'}
    api, worker = os.environ['CAD_NATIVE_E2E_API'], os.environ['CAD_NATIVE_E2E_WORKER']
    assert api.startswith('cad-coedit-') and api.endswith('-api')
    assert worker.startswith('cad-coedit-') and worker.endswith('-worker')
    host = json.loads(Path(os.environ['CAD_NATIVE_E2E_HOST_ENV']).read_text())
    from dotenv import dotenv_values
    environment = dict(dotenv_values(os.environ['CAD_NATIVE_E2E_ENV']))
    assert environment['DURABLE_CONTROL_PLANE_ENABLED'].lower() == 'true'
    private = json.loads(Path(os.environ['CAD_NATIVE_E2E_PRIVATE']).read_text())
    source_db = urlsplit(host['DATABASE_URL']).path.lstrip('/')
    assert source_db.startswith('cad_coedit_live_') and environment['OBJECT_STORE_BUCKET'].startswith('cad-coedit-live-')
    assert source_db == urlsplit(environment['DATABASE_URL']).path.lstrip('/')
    podman = os.getenv('CAD_NATIVE_E2E_PODMAN', '/opt/homebrew/bin/podman')
    def run(*args, **kwargs):
        result = subprocess.run([podman, *args], capture_output=True, **kwargs)
        if result.returncode:
            (out / 'last-command.stderr').write_bytes(result.stderr)
            raise RuntimeError('Container command failed; inspect private last-command.stderr')
        return result.stdout
    api_info = json.loads(run('inspect', api))[0]
    assert api_info['State']['Running'] and json.loads(run('inspect', worker))[0]['State']['Running']
    assert urlsplit(host['DATABASE_URL']).hostname in {'localhost','127.0.0.1'}
    pg_container = os.environ.get('CAD_NATIVE_E2E_POSTGRES', 'cad-agent_postgres_1')
    pg_user = unquote(urlsplit(host['DATABASE_URL']).username or '')
    assert re.fullmatch(r'[a-z_][a-z0-9_]*', pg_user)
    suffix = uuid4().hex[:12]
    target_db, target_bucket = 'cad_coedit_restore_' + suffix, 'cad-coedit-restore-' + suffix
    clone = 'cad-coedit-restore-' + suffix + '-api'
    before_http = http_facts(base, private)
    save(out / 'http-before.json', before_http)
    source_stopped = False
    try:
        print('Quiescing only the isolated source API and worker', flush=True)
        run('stop', '--time', '30', api, worker)
        source_stopped = True
        source_facts = asyncio.run(database_facts(host['DATABASE_URL']))
        save(out / 'database-before.json', source_facts)
        # pg_dump reads the quiescent source, without locks/changes to any other DB.
        with (out / 'postgres.dump').open('xb') as handle:
            subprocess.run([podman, 'exec', pg_container, 'pg_dump', '-U', pg_user,
                '-d', source_db, '--format=custom'], check=True, stdout=handle, stderr=subprocess.PIPE)
        auth_counts = {table:source_facts['tables'][table]['rows']
            for table in ('auth_users', 'auth_sessions', 'auth_invite_codes')}
        assert auth_counts['auth_users'] > 0 and auth_counts['auth_sessions'] > 0
        command = ['run', '--rm', '--security-opt', 'label=disable', '--network', 'cad-agent_default',
            '--env-file', os.environ['CAD_NATIVE_E2E_ENV'], '-e', 'PYTHONDONTWRITEBYTECODE=1',
            '-e', 'PYTHONPATH=/app/backend',
            '-v', str(root) + ':/app:ro', '-v', str(out) + ':' + str(out),
            os.getenv('CAD_NATIVE_E2E_TEST_IMAGE', 'localhost/cad-agent-pytest:coedit-20260913'),
            'python', 'tests/e2e/native_joint_restore.py']
        run(*command, '--objects', 'backup', '--out', str(out))
        assert asyncio.run(database_facts(host['DATABASE_URL'])) == source_facts
        print('Consistent PostgreSQL and object backup captured', flush=True)
    finally:
        # Restart both even if the stop command partially succeeded.
        run('start', api, worker)
    assert source_stopped
    async def create_empty():
        connection = await asyncpg.connect(host['DATABASE_URL'].replace('postgresql+asyncpg:', 'postgresql:'))
        try:
            assert not await connection.fetchval('SELECT 1 FROM pg_database WHERE datname=$1', target_db)
            await connection.execute('CREATE DATABASE ' + target_db)
        finally:
            await connection.close()
    asyncio.run(create_empty())
    with (out / 'postgres.dump').open('rb') as handle:
        subprocess.run([podman, 'exec', '-i', pg_container, 'pg_restore', '-U', pg_user,
            '-d', target_db, '--exit-on-error', '--no-owner'], check=True, stdin=handle, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    restored_facts = asyncio.run(database_facts(with_database(host['DATABASE_URL'], target_db)))
    save(out / 'database-restored.json', restored_facts)
    assert restored_facts == source_facts, 'row, schema, sequence or RLS mismatch'
    print('Restored database rows, schema, sequences and RLS match', flush=True)
    run(*command, '--objects', 'restore', '--out', str(out), '--target-bucket', target_bucket)
    print('Restored object bytes and metadata verified', flush=True)
    state = out / 'clone-state'
    state.mkdir(mode=0o700)
    environment.update(DATABASE_URL=with_database(environment['DATABASE_URL'], target_db),
        OBJECT_STORE_BUCKET=target_bucket, TEMPORAL_TASK_QUEUE='cad-coedit-restore-' + suffix,
        TEMPORAL_AGENT_V2_TASK_QUEUE='cad-coedit-restore-agent-' + suffix,
        DATABASE_POOL_SIZE='2', DATABASE_MAX_OVERFLOW='0', FILE_STORAGE_DIR=str(state / 'files'),
        HISTORY_DB_PATH=str(state / 'history.db'), FUSION_AGENT_DB_PATH=str(state / 'fusion-audit.db'),
        FUSION_TOKEN_DB_PATH=str(state / 'fusion-tokens.db'), FUSION_AGENT_ARTIFACT_DIR=str(state / 'fusion-artifacts'))
    envfile = out / 'clone.env'
    assert all(value is not None and '\n' not in value for value in environment.values())
    envfile.write_text('\n'.join(key + '=' + value for key, value in environment.items()) + '\n')
    envfile.chmod(0o600)
    # No clone worker is started; original queued executions cannot run against
    # the restored DB. This drill verifies recovered data/read routes only.
    run('run', '-d', '--name', clone, '--security-opt', 'label=disable', '--network', 'cad-agent_default',
        '-p', '127.0.0.1::8000', '--env-file', str(envfile),
        '-v', str(root / 'backend') + ':/app/backend:ro', '-v', str(state) + ':' + str(state),
        '-v', '/run/user/501/podman/podman.sock:/var/run/docker.sock', api_info['ImageName'],
        'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000')
    try:
        port = json.loads(run('inspect', clone))[0]['NetworkSettings']['Ports']['8000/tcp'][0]['HostPort']
        clone_base = 'http://127.0.0.1:' + port
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                response = httpx.get(clone_base + '/health', timeout=3)
                if response.status_code == 200:
                    break
            except httpx.RequestError:
                pass
            time.sleep(1)
        else:
            raise AssertionError('restored API did not boot')
        restored_http = http_facts(clone_base, private)
        save(out / 'http-restored.json', restored_http)
        assert restored_http == before_http
        login = httpx.post(clone_base + '/api/auth/login/password', timeout=60,
            json={'phone':private['owner']['phone'], 'password':private['owner']['password']})
        login.raise_for_status()
        recovered_user = {**private, 'owner':{**private['owner'], 'token':login.json()['token']}}
        assert http_facts(clone_base, recovered_user) == before_http
        assert http_facts(base, private) == before_http, 'source models changed during recovery drill'
        report = {'status':'passed', 'source_database':source_db, 'restored_database':target_db,
            'restored_bucket':target_bucket, 'restored_api':clone, 'table_count':len(source_facts['tables']),
            'row_count':sum(x['rows'] for x in source_facts['tables'].values()),
            'all_rows_schema_sequences_rls_equal':True, 'auth_rows':auth_counts,
            'original_session_and_fresh_password_login_verified':True,
            'postgres_backup_sha256':digest(out / 'postgres.dump'),
            'objects':json.loads((out / 'object-restore.json').read_text()),
            'historical_revisions_downloaded':len(restored_http['revisions']),
            'head_revision_id':restored_http['head_revision_id'], 'state_version':restored_http['state_version'],
            'all_historical_download_hashes_equal':True, 'source_preserved':True,
            'scope':'quiesced MCAD PostgreSQL including auth + current S3 versions including immutable history; Temporal history replay is separate; no claim of in-flight Temporal DB restore or Fusion local runtime recovery'}
        save(out / 'report.json', report)
        print('CAD_JOINT_RESTORE=' + json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        run('stop', '--time', '15', clone)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--objects', choices=['backup', 'restore'])
    parser.add_argument('--out', type=Path)
    parser.add_argument('--target-bucket')
    args = parser.parse_args()
    if args.objects:
        objects(args.objects, args.out, args.target_bucket)
    else:
        main()
