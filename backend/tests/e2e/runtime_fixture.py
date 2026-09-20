"""Execute real fixture setup in an explicitly selected test backend runtime."""
import json
import os
import subprocess


def run_native_seed(source: str, arguments: list[str]):
    local = os.environ.get('CAD_NATIVE_E2E_COMMAND')
    if local:
        command = json.loads(local)
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise ValueError('CAD_NATIVE_E2E_COMMAND must be a command argument array')
    else:
        container = os.environ['CAD_NATIVE_E2E_API']
        if not (container.startswith('cad-coedit-') and container.endswith('-api')):
            raise ValueError('Select an isolated CAD acceptance backend')
        command = [os.getenv('CAD_NATIVE_E2E_PODMAN', '/opt/homebrew/bin/podman'), 'exec', '-i', container, 'python']
    return subprocess.run([*command, '-', *arguments], input=source, text=True, capture_output=True, check=False)
