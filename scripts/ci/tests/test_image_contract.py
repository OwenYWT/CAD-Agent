"""Archive identity tests; these fixtures do not claim native runtime success."""
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts.ci.image_manifest import IMAGES, create, digest


class ImageArchiveContract(unittest.TestCase):
    def archive(self, root, name, sha, label=None):
        contents = {'config.json': json.dumps({'config': {'Labels': {'org.opencontainers.image.revision': label or sha}}}).encode(),
            'manifest.json': json.dumps([{'Config': 'config.json', 'RepoTags': ['cad-agent-'+name+':ci-'+sha], 'Layers': []}]).encode()}
        with tarfile.open(root/(name+'.tar'), 'w') as archive:
            for key, value in contents.items():
                info = tarfile.TarInfo(key); info.size = len(value)
                archive.addfile(info, io.BytesIO(value))

    def test_all_image_archives_are_bound_to_build_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in IMAGES: self.archive(root, name, 'a'*40)
            result=create(root, 'a'*40)
            self.assertEqual(set(result['images']), set(IMAGES))
            self.assertEqual(result['images']['sandbox']['sha256'], digest(root/'sandbox.tar'))

    def test_stale_application_image_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in IMAGES: self.archive(root, name, 'a'*40, 'b'*40 if name=='backend' else None)
            with self.assertRaisesRegex(ValueError, 'wrong image source'):
                create(root, 'a'*40)

    def test_missing_archive_is_not_replaced_by_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.archive(root, 'sandbox', 'a'*40)
            with self.assertRaises(FileNotFoundError):create(root, 'a'*40)
