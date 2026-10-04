"""Bind exported images to their build SHA; validate bytes before loading them."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

IMAGES = ('sandbox', 'object-store', 'backend', 'frontend')


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def image_config(path):
    with tarfile.open(path) as archive:
        manifests = json.load(archive.extractfile('manifest.json'))
        if len(manifests) != 1:
            raise ValueError('exactly one image is required per archive')
        entry = manifests[0]
        raw = archive.extractfile(entry['Config']).read()
        config = json.loads(raw)
        return entry, config, 'sha256:' + hashlib.sha256(raw).hexdigest()


def create(root, sha):
    records = {}
    for name in IMAGES:
        path = root / (name + '.tar')
        entry, config, identity = image_config(path)
        if config['config']['Labels'].get('org.opencontainers.image.revision') != sha:
            raise ValueError('wrong image source: ' + name)
        tags = entry['RepoTags']
        if len(tags) != 1 or not tags[0].endswith(':ci-' + sha):
            raise ValueError('image tag is not bound to source: ' + name)
        records[name] = {'file': path.name, 'sha256': digest(path), 'image_id': identity, 'tag': tags[0]}
    result = {'schema_version': 'cad-ci-images.v1', 'source_sha': sha, 'images': records}
    (root / 'images.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def load(root, sha, names, runtime):
    manifest = json.loads((root / 'images.json').read_text())
    if manifest['source_sha'] != sha or set(manifest['images']) != set(IMAGES):
        raise ValueError('incomplete or stale image manifest')
    for name in names:
        row = manifest['images'][name]
        path = root / row['file']
        if path.resolve().parent != root.resolve() or digest(path) != row['sha256']:
            raise ValueError('image archive changed: ' + name)
        subprocess.run([runtime, 'load', '--input', str(path)], check=True)
        actual = subprocess.check_output([runtime, 'image', 'inspect', row['tag'], '--format', '{{.Id}}'], text=True).strip()
        if actual != row['image_id']:
            raise ValueError('loaded image differs: ' + name)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('create', 'load'))
    parser.add_argument('root', type=Path)
    parser.add_argument('--sha', default=os.environ.get('GITHUB_SHA'))
    parser.add_argument('--images', nargs='+', choices=IMAGES, default=list(IMAGES))
    parser.add_argument('--runtime', default=os.getenv('SANDBOX_COMMAND', 'docker'))
    args = parser.parse_args()
    if not args.sha:
        parser.error('--sha is required outside Actions')
    value = create(args.root, args.sha) if args.mode == 'create' else load(args.root, args.sha, args.images, args.runtime)
    if args.mode == 'load' and os.getenv('GITHUB_ENV'):
        mapping = {'sandbox': 'SANDBOX_IMAGE', 'object-store': 'CAD_CI_OBJECT_STORE_IMAGE',
                   'backend': 'BACKEND_IMAGE', 'frontend': 'FRONTEND_IMAGE'}
        with Path(os.environ['GITHUB_ENV']).open('a') as stream:
            for name in args.images:
                stream.write(mapping[name] + '=' + value['images'][name]['tag'] + '\n')
            if 'backend' in args.images:
                stream.write('MONITORING_IMAGE=' + value['images']['backend']['tag'] + '\n')
    print('Verified source and bytes of ' + ', '.join(value['images']))


if __name__ == '__main__':
    main()
