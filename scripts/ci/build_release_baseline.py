"""Build an immutable released source for the mandatory image transition drill.

Reuse a verified runtime only when its build inputs are byte-identical. All
application files are cleared and replaced; this never overlays old and new code.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BASELINE = 'df8fdbb84f34164d9ad18e9101f881de4f0fa17a'
KINDS = ('backend', 'frontend', 'sandbox')


def build_baseline(report: Path) -> dict[str, str]:
    runtime = os.getenv('SANDBOX_COMMAND', 'docker')
    configured = {kind: os.getenv('CAD_CI_BASELINE_' + kind.upper() + '_IMAGE') for kind in KINDS}
    if any(configured.values()):
        if not all(configured.values()):
            raise ValueError('Select all three baseline images, not a mixed release')
        return {'CAD_CI_BASELINE_' + kind.upper() + '_IMAGE': image for kind, image in configured.items()}
    evidence = report / 'baseline-build'
    evidence.mkdir(exist_ok=False)
    client_env = None
    def run(args, **kwargs):
        return subprocess.run(args, check=True, text=True, env=client_env, **kwargs)
    def identical(old, paths):
        return all((old / p).read_bytes() == (ROOT / p).read_bytes() for p in paths)
    def digest_files(root):
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*') if p.is_file() and 'node_modules' not in p.parts and '__pycache__' not in p.parts}
    images = {kind: f'cad-ci-release-{kind}:{BASELINE}' for kind in KINDS}
    bases = {kind: run([runtime, 'image', 'inspect', os.environ[kind.upper() + '_IMAGE'], '--format', '{{.Id}}'], capture_output=True).stdout.strip() for kind in KINDS}
    with tempfile.TemporaryDirectory(prefix='cad-ci-release-source-', dir=os.getenv('RUNNER_TEMP')) as temporary:
        old = Path(temporary)
        client_env = dict(os.environ, DOCKER_CONFIG=str(old / '.ci-docker-client'))
        base_tags = {kind: 'cad-ci-verified-' + kind + ':' + digest.removeprefix('sha256:') for kind, digest in bases.items()}
        for kind, tag in base_tags.items():
            run([runtime, 'tag', bases[kind], tag])
        archive = subprocess.check_output(['git', '-C', str(ROOT), 'archive', '--format=tar', BASELINE])
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(old, filter='data')
        modes = {}
        backend_same = identical(old, ['backend/Dockerfile', 'backend/requirements.txt', 'backend/requirements.lock'])
        sandbox_dockerfile = (old / 'backend/sandbox/Dockerfile').read_text()
        copies = re.findall(r'^COPY (\S+) (/opt/cad-agent/\S+)$', sandbox_dockerfile, re.M)
        without_app_copies = lambda text: re.sub(r'^COPY \S+ /opt/cad-agent/\S+\n', '', text, flags=re.M)
        sandbox_same = (identical(old, ['backend/sandbox/runtime-lock.json', 'backend/sandbox/runtime-requirements.txt',
            'backend/sandbox/patch_build123d.py', 'backend/sandbox/freecad_build_verify.py'])
            and without_app_copies(sandbox_dockerfile) == without_app_copies((ROOT / 'backend/sandbox/Dockerfile').read_text())
            and digest_files(old / 'third_party/cadskills') == digest_files(ROOT / 'third_party/cadskills'))
        recipes = {}
        if backend_same:
            recipes['backend'] = (ROOT / 'backend/Dockerfile.reuse-dependencies').read_text().replace(
                'ARG VERIFIED_BACKEND_IMAGE\nFROM ${VERIFIED_BACKEND_IMAGE}', 'FROM ' + base_tags['backend'])
        if sandbox_same:
            recipes['sandbox'] = ('FROM ' + base_tags['sandbox'] + '\nUSER 0\nRUN rm -rf /opt/cad-agent\n'
                + ''.join(f'COPY {source} {target}\n' for source, target in copies)
                + 'RUN chmod -R a+rX /opt/cad-agent\nUSER 1000:1000\n')
        for kind in ('backend', 'sandbox'):
            dockerfile = old / ('baseline-' + kind + '.Dockerfile')
            if kind in recipes:
                dockerfile.write_text(recipes[kind])
                modes[kind] = 'verified_identical_runtime_inputs_replaced_application'
            else:
                dockerfile = old / ('backend/Dockerfile' if kind == 'backend' else 'backend/sandbox/Dockerfile')
                modes[kind] = 'clean_released_dockerfile'
            with (evidence / (kind + '.log')).open('w') as log:
                run([runtime, 'build', '--label', 'org.opencontainers.image.revision=' + BASELINE,
                     '-f', str(dockerfile), '-t', images[kind], str(old)], stdout=log, stderr=subprocess.STDOUT)
        # The PR already installed this exact lockfile. Use the same packages,
        # but compile the archived frontend source and its own configuration.
        if not identical(old, ['.node-version']):
            raise ValueError('The baseline needs its pinned Node version; provide separately built baseline images')
        same_lock = identical(old, ['frontend/package.json', 'frontend/package-lock.json'])
        if same_lock:
            (old / 'frontend/node_modules').symlink_to(ROOT / 'frontend/node_modules', target_is_directory=True)
        with (evidence / 'frontend-assets.log').open('w') as log:
            if not same_lock:
                run(['npm', 'ci', '--prefix', str(old / 'frontend')], stdout=log, stderr=subprocess.STDOUT)
            run(['npm', 'run', 'build', '--prefix', str(old / 'frontend')], stdout=log, stderr=subprocess.STDOUT)
        frontend_dockerfile = old / 'deploy/tencent/baseline-frontend.Dockerfile'
        if identical(old, ['deploy/tencent/Dockerfile.frontend']):
            frontend_recipe = ('FROM ' + base_tags['frontend'] + '\nRUN rm -rf /usr/share/nginx/html/*\n'
                'COPY dist/ /usr/share/nginx/html/\nCOPY nginx.conf /etc/nginx/templates/default.conf.template\n')
        else:
            frontend_recipe = (old / 'deploy/tencent/Dockerfile.frontend').read_text()
        frontend_dockerfile.write_text(frontend_recipe)
        Path(str(frontend_dockerfile) + '.dockerignore').write_text('**\n!dist\n!dist/**\n!nginx.conf\n')
        with (evidence / 'frontend.log').open('w') as log:
            run([runtime, 'build', '--network', 'none', '--label', 'org.opencontainers.image.revision=' + BASELINE,
                 '-f', str(frontend_dockerfile), '-t', images['frontend'], str(old / 'frontend')], stdout=log, stderr=subprocess.STDOUT)
        modes['frontend'] = 'released_assets_compiled_with_locked_dependencies'
        result = {'passed': True, 'source_revision': BASELINE, 'modes': modes, 'runtime_bases': bases,
            'images': {kind: run([runtime, 'image', 'inspect', tag, '--format', '{{.Id}}'], capture_output=True).stdout.strip() for kind, tag in images.items()},
            'frontend_dist': digest_files(old / 'frontend/dist')}
        (evidence / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
    return {'CAD_CI_BASELINE_' + kind.upper() + '_IMAGE': image for kind, image in images.items()}


if __name__ == '__main__':
    print(json.dumps(build_baseline(Path(os.environ['CAD_CI_REPORT_ROOT'])), indent=2))
