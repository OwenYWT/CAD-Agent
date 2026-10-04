"""Disposable delivery test using the shipping Compose file and built images.

Only the test project's own containers/volumes are removed. There are no source
mounts in the application containers and no credentials/paid provider calls.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[2]


def main():
    scope = os.environ['CAD_CI_SCOPE']
    if not scope.startswith('cad-ci-') or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in scope):
        raise ValueError('select an isolated cad-ci project')
    runtime = os.getenv('SANDBOX_COMMAND', 'docker')
    report = Path(os.environ['CAD_CI_REPORT_ROOT'])
    report.mkdir(parents=True, exist_ok=True)
    private = Path(tempfile.mkdtemp(prefix=scope + '-', dir=os.getenv('RUNNER_TEMP')))
    private.chmod(0o700)
    work = private / 'work'
    work.mkdir(mode=0o755)
    password, monitor_password, monitor_auth_password = secrets.token_hex(24), secrets.token_hex(24), secrets.token_hex(24)
    env = dict(os.environ, POSTGRES_PASSWORD=password, MINIO_ROOT_USER='cad_ci',
               MINIO_ROOT_PASSWORD=password)
    owner = secrets.token_hex(24)
    env['COMPOSE_PROJECT_NAME'] = scope
    # Explicit -p overrides the shipping file's fixed production project name.
    compose = [runtime, 'compose', '-p', scope, '-f', str(private / 'compose.yml'),
               '-f', str(private / 'ci.override.yml')]

    def run(args, **kwargs):
        return subprocess.run(args, env=env, check=True, text=True, **kwargs)

    if run([runtime, 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + scope], capture_output=True).stdout.strip():
        raise ValueError('refusing to reuse an existing Compose project')
    if run([runtime, 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + scope], capture_output=True).stdout.strip():
        raise ValueError('refusing to reuse existing Compose volumes')
    shutil.copyfile(ROOT / 'deploy/tencent/compose.yml', private / 'compose.yml')
    config = {
        'APP_ENVIRONMENT': 'test', 'AUTH_REQUIRED': 'true', 'AUTH_TOKEN_SECRET': secrets.token_hex(32),
        'ADMIN_PASSWORD': secrets.token_urlsafe(24), 'DURABLE_CONTROL_PLANE_ENABLED': 'true',
        'DATABASE_URL': f'postgresql+asyncpg://cad_native:{password}@postgres:5432/cad_native',
        'OBJECT_STORE_ENDPOINT_URL': 'http://minio:9000', 'OBJECT_STORE_ACCESS_KEY': 'cad_ci',
        'OBJECT_STORE_SECRET_KEY': password, 'OBJECT_STORE_BUCKET': 'cad-native-artifacts',
        'TEMPORAL_TARGET': 'temporal:7233', 'TEMPORAL_NAMESPACE': 'cad-native',
        'TEMPORAL_TASK_QUEUE': scope + '-v1', 'TEMPORAL_AGENT_V2_TASK_QUEUE': scope + '-v2',
        'SANDBOX_IMAGE': env['SANDBOX_IMAGE'], 'SANDBOX_RUNTIME': 'docker', 'SANDBOX_COMMAND': 'docker',
        'TMPDIR': str(work), 'SANDBOX_MAX_CONCURRENT': '1', 'RATE_LIMIT_PER_MINUTE': '500',
        'MOONSHOT_API_KEY': '', 'DASHSCOPE_API_KEY': '', 'AZURE_OPENAI_API_KEY': '',
    }
    monitor = dict(config, DATABASE_URL=f'postgresql+asyncpg://cad_ci_monitor_auth:{monitor_auth_password}@postgres:5432/cad_native',
        MONITOR_DATABASE_URL=f'postgresql+asyncpg://cad_ci_monitor:{monitor_password}@postgres:5432/cad_native')
    for name, values in [('backend.env', config), ('monitor.env', monitor)]:
        path = private / name
        path.write_text(''.join(k + '=' + v + '\n' for k, v in values.items()))
        path.chmod(0o600)
    (private / '.env').write_text('')
    certs = private / 'certs'
    certs.mkdir(mode=0o755)
    run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
         '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
         '-keyout', str(certs/'privkey.pem'), '-out', str(certs/'fullchain.pem')], capture_output=True)
    nginx = (ROOT/'deploy/tencent/nginx.conf').read_text()
    for before, after in [('127.0.0.1:59100', 'minio:9000'), ('127.0.0.1:8091', 'frontend:80'),
                          ('/www/server/panel/vhost/cert/www.wordswave.ai/', '/ci/certs/')]:
        if before not in nginx:
            raise ValueError('shipping Nginx topology changed: ' + before)
        nginx = nginx.replace(before, after)
    (private/'nginx.conf').write_text(nginx)
    # The missing LLM is intentionally not reported ready. The exact durable
    # readiness contract used by browser CI is checked instead.
    health = "import json,urllib.request,urllib.error; exec(\"try:\\n r=urllib.request.urlopen('http://localhost:8000/ready')\\nexcept urllib.error.HTTPError as e:\\n r=e\"); assert json.load(r)['durable_control_plane']['status']=='ready'"
    override = {'services': {}}
    for service in ('backend', 'workflow-worker', 'migrate'):
        override['services'][service] = {'volumes': [f'{work}:{work}'], 'labels': {'cad.ci.owner': owner}, 'restart': 'no'}
    override['services']['backend']['healthcheck'] = {'test': ['CMD', 'python', '-c', health]}
    override['services']['minio'] = {'image': env['CAD_CI_OBJECT_STORE_IMAGE'],
        'healthcheck': {'test': ['CMD', 'curl', '-f', 'http://localhost:9000/minio/health/live']}}
    override['services']['minio-init'] = {'image': env['CAD_CI_OBJECT_STORE_IMAGE']}
    override['services']['edge'] = {'image': env['FRONTEND_IMAGE'],
        'entrypoint': ['nginx', '-g', 'daemon off;'],
        'volumes': [f'{private}/nginx.conf:/etc/nginx/conf.d/default.conf:ro', f'{certs}:/ci/certs:ro'],
        'ports': ['127.0.0.1:18443:443'], 'depends_on': ['frontend', 'minio'], 'restart': 'no'}
    # Reset the fixed production work mount; retain only this project's volume.
    for service in ('backend', 'workflow-worker', 'migrate'):
        socket = os.getenv('CAD_CI_DOCKER_SOCKET', '/var/run/docker.sock')
        override['services'][service]['volumes'] = [f'{socket}:/var/run/docker.sock',
            'cad_data:/app/data', f'{work}:{work}']
        if os.getenv('CAD_CI_ENGINE_SECURITY_OPT'):
            override['services'][service]['security_opt'] = [os.environ['CAD_CI_ENGINE_SECURITY_OPT']]
    # Compose !override avoids merging the production ports/mounts into CI.
    raw = yaml.safe_dump(override, sort_keys=False)
    raw = raw.replace('    volumes:\n', '    volumes: !override\n')
    ports = {'backend': '18060:8000', 'frontend': '18160:80', 'minio': '19090:9000', 'monitoring': '18061:8000'}
    value = yaml.safe_load(raw.replace(' !override', ''))
    for service, port in ports.items():
        value['services'].setdefault(service, {})['ports'] = ['127.0.0.1:' + port]
    raw = yaml.safe_dump(value, sort_keys=False).replace('    volumes:\n', '    volumes: !override\n').replace('    ports:\n', '    ports: !override\n')
    (private / 'ci.override.yml').write_text(raw)
    started = False
    try:
        run([*compose, 'config', '--quiet'])
        started = True
        run([*compose, 'up', '-d', '--no-build', 'postgres', 'minio-init', 'temporal', 'migrate'])
        # Wait for the actual migration exit code, then provision a restricted
        # monitoring query identity; never substitute backend.env for monitor.env.
        migrate = run([*compose, 'ps', '-aq', 'migrate'], capture_output=True).stdout.strip()
        assert run([runtime, 'wait', migrate], capture_output=True).stdout.strip() == '0'
        # Authentication also resolves the user's persisted tenant/principal
        # through the RLS runtime role. Usage queries use a different login
        # which cannot assume either authentication or runtime roles.
        sql = f"CREATE ROLE cad_ci_monitor LOGIN PASSWORD '{monitor_password}' NOSUPERUSER NOBYPASSRLS; GRANT cad_agent_monitor TO cad_ci_monitor; CREATE ROLE cad_ci_monitor_auth LOGIN PASSWORD '{monitor_auth_password}' NOSUPERUSER NOBYPASSRLS; GRANT cad_agent_auth, cad_agent_runtime TO cad_ci_monitor_auth;"
        run([*compose, 'exec', '-T', 'postgres', 'psql', '-v', 'ON_ERROR_STOP=1', '-U', 'cad_native', '-d', 'cad_native'], input=sql)
        run([*compose, 'up', '-d', '--no-build', '--wait', 'backend', 'workflow-worker', 'frontend', 'monitoring', 'edge'])
        run([*compose, 'exec', '-T', 'edge', 'nginx', '-t'])
        monitor_container = run([*compose, 'ps', '-q', 'monitoring'], capture_output=True).stdout.strip()
        source = (ROOT/'scripts/ci/monitor_database_contract.py').read_text()
        checked = run([runtime,'exec','-i',monitor_container,'python','-'], input=source, capture_output=True)
        result = json.loads(checked.stdout)
        assert result['passed'] is True
        (report/'monitor-database.json').write_text(json.dumps(result,indent=2))
        api = run([*compose, 'ps', '-q', 'backend'], capture_output=True).stdout.strip()
        inspected = json.loads(run([runtime, 'inspect', api], capture_output=True).stdout)[0]
        assert inspected['Config']['Image'] == env['BACKEND_IMAGE']
        assert all('/workspace' not in m.get('Destination', '') and '/app/backend' not in m.get('Destination', '') for m in inspected['Mounts'])
        # Browser fixtures execute inside the delivered backend, while Playwright
        # and HTTP assertions run outside it through the shipped frontend Nginx.
        test_env = dict(env, CAD_NATIVE_E2E_URL='http://127.0.0.1:18160', CAD_NATIVE_E2E_WEB='http://127.0.0.1:18160',
            CAD_NATIVE_E2E_PRIVATE=str(private/'accounts.json'), CAD_BROWSER_CONTRACT_REPORT=str(report/'packaged-browser'),
            CAD_NATIVE_E2E_COMMAND=json.dumps([runtime, 'exec', '-i', api, 'python']),
            CAD_CI_ADMIN_PASSWORD=config['ADMIN_PASSWORD'], CAD_CI_DEPLOY_REPORT=str(report/'delivery.json'),
            CAD_CI_TLS_URL='https://localhost:18443', CAD_CI_TLS_CA=str(certs/'fullchain.pem'),
            CAD_CI_DEPLOY_PRIVATE=str(private), CAD_CI_DEPLOY_COMPOSE=json.dumps(compose))
        subprocess.run([sys.executable, str(ROOT/'scripts/ci/deploy_http_contract.py'), 'accounts'], env=test_env, check=True)
        subprocess.run([sys.executable, str(ROOT/'backend/tests/e2e/browser_contract.py')], env=test_env, check=True)
        subprocess.run([sys.executable, str(ROOT/'scripts/ci/deploy_http_contract.py'), 'permissions'], env=test_env, check=True)
        (report/'compose.json').write_text(json.dumps({'passed': True, 'source_sha': env.get('GITHUB_SHA'),
            'compose_sha256': hashlib.sha256((ROOT/'deploy/tencent/compose.yml').read_bytes()).hexdigest(),
            'application_image': inspected['Image'], 'source_mounts': False, 'project': scope,
            'provider_evaluation': False, 'monitor_has_separate_credentials': True}, indent=2))
    finally:
        if started:
            with (report/'compose.log').open('w') as log:
                subprocess.run([*compose, 'logs', '--no-color'], env=env, stdout=log, stderr=subprocess.STDOUT)
            # This is a new disposable project proven absent above, never the
            # production cad-native project. No global prune or down -v is used.
            volumes = run([runtime, 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + scope], capture_output=True).stdout.split()
            run([*compose, 'down', '--remove-orphans'])
            for volume in volumes:
                run([runtime, 'volume', 'rm', volume])
        shutil.rmtree(private)


if __name__ == '__main__':
    main()
