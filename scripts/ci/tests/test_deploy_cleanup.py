"""CI cleanup stays within scratch mounts and must not hide failures."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.ci.deploy_contract import remove_private_directory


class DeployCleanup(unittest.TestCase):
    def private(self):
        temporary = tempfile.TemporaryDirectory(prefix='cad-ci-cleanup-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / 'private'
        (root / 'work').mkdir(parents=True)
        return root

    def test_writable_scratch_does_not_use_privileged_cleanup(self):
        root = self.private()
        (root / 'work' / 'artifact').write_text('test output')
        with patch('scripts.ci.deploy_contract.subprocess.run') as run:
            remove_private_directory(root, 'docker', 'verified-backend')
            run.assert_not_called()
        self.assertFalse(root.exists())

    def test_foreign_owned_scratch_uses_only_its_work_mount(self):
        root = self.private()
        with patch('scripts.ci.deploy_contract.shutil.rmtree', side_effect=[PermissionError('foreign uid'), None]) as remove, patch('scripts.ci.deploy_contract.subprocess.run') as run:
            remove_private_directory(root, 'docker', 'verified-backend')
        args = run.call_args.args[0]
        self.assertIn(str(root / 'work') + ':/ci-work:rw', args)
        self.assertEqual(args[args.index('--network') + 1], 'none')
        self.assertIn('--read-only', args)
        self.assertEqual(args[args.index('--cap-drop') + 1], 'ALL')
        self.assertEqual(args[args.index('--cap-add') + 1], 'DAC_OVERRIDE')
        self.assertNotIn('--privileged', args)
        self.assertFalse(any('docker.sock' in arg for arg in args))
        self.assertTrue(run.call_args.kwargs['check'])
        self.assertEqual(remove.call_count, 2)

    def test_cleanup_failure_still_fails_the_job(self):
        root = self.private()
        with patch('scripts.ci.deploy_contract.shutil.rmtree', side_effect=PermissionError('foreign uid')) as remove, patch('scripts.ci.deploy_contract.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['docker'])):
            with self.assertRaises(subprocess.CalledProcessError):
                remove_private_directory(root, 'docker', 'verified-backend')
        self.assertEqual(remove.call_count, 1)

    def test_work_symlink_cannot_select_an_outside_cleanup_mount(self):
        root = self.private()
        outside = root.parent / 'outside'
        outside.mkdir()
        (root / 'work').rmdir()
        (root / 'work').symlink_to(outside, target_is_directory=True)
        with patch('scripts.ci.deploy_contract.shutil.rmtree', side_effect=PermissionError('foreign uid')), patch('scripts.ci.deploy_contract.subprocess.run') as run:
            with self.assertRaises(PermissionError):
                remove_private_directory(root, 'docker', 'verified-backend')
            run.assert_not_called()
