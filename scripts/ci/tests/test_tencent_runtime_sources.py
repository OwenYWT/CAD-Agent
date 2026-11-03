"""The cloud delivery path must include and verify the native runtime's code.

These tests inspect packaging inputs, not geometry outcomes. Actual image byte
checks and the restricted native probe remain required before deployment.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import sys


ROOT = Path(__file__).resolve().parents[3]


def runtime_copies(path):
    result = {}
    for line in path.read_text().splitlines():
        if not line.startswith('COPY '):
            continue
        words = shlex.split(line, comments=True)
        if not words or words[0] != 'COPY':
            continue
        args = [w for w in words[1:] if not w.startswith('--')]
        if len(args) == 2 and args[0].startswith('backend/') and args[1].startswith('/opt/cad-agent/'):
            result[args[1]] = args[0]
    return result


def test_tencent_delivery_includes_every_declared_runtime_file():
    expected = runtime_copies(ROOT/'backend/sandbox/Dockerfile')
    delivered = runtime_copies(ROOT/'deploy/tencent/Dockerfile.sandbox')
    assert expected
    assert delivered == expected, {'missing_or_wrong': {k:v for k,v in expected.items() if delivered.get(k) != v}}


def test_tencent_image_verifier_covers_every_declared_runtime_file(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('tencent_verify_images', ROOT/'deploy/tencent/verify-images.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = runtime_copies(ROOT/'backend/sandbox/Dockerfile')
    files = [{'path':p, 'sha256':hashlib.sha256((ROOT/p).read_bytes()).hexdigest()} for p in expected.values()]
    manifest = tmp_path/'manifest.json';manifest.write_text(json.dumps({'files':files}))
    captured = {}
    def capture(image, sources):
        captured[image] = sources
        return {'inventory_only':True}
    monkeypatch.setattr(module, 'check_image', capture)
    monkeypatch.setattr(sys, 'argv', ['verify-images', '--manifest', str(manifest),
        '--backend-image', 'backend', '--sandbox-image', 'sandbox', '--report', str(tmp_path/'report.json')])
    module.main()
    assert captured['sandbox'] == {target:hashlib.sha256((ROOT/source).read_bytes()).hexdigest()
                                  for target, source in expected.items()}
