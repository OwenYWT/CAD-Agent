"""Resource preparation must never delete tools on developer/owned machines."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'prepare_hosted_runner.sh'


class HostedRunnerResources(unittest.TestCase):
    def refused(self, actions, runner):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'cleanup-attempted'
            sudo = root / 'sudo'
            sudo.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 99\n')
            sudo.chmod(0o755)
            environment = dict(os.environ, GITHUB_ACTIONS=actions,
                               RUNNER_ENVIRONMENT=runner, RUNNER_OS='Linux',
                               PATH=str(root) + os.pathsep + os.environ['PATH'])
            result = subprocess.run(['bash', str(SCRIPT)], env=environment,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn('Refusing', result.stderr)
            self.assertFalse(marker.exists(), 'resource cleanup ran on an owned machine')

    def test_local_preparation_cannot_delete_tools(self):
        self.refused('false', 'github-hosted')

    def test_self_hosted_preparation_cannot_delete_tools(self):
        self.refused('true', 'self-hosted')
