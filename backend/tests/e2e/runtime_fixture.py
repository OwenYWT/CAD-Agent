"""Execute real fixture setup in an explicitly selected test backend runtime."""
import json
import math
import os
import subprocess
import time


def task_wait_seconds():
    """One fixture deadline for the full durable chain, not one CAD activity."""
    seconds = float(os.getenv('CAD_TASK_STATE_TIMEOUT', '900'))
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('CAD_TASK_STATE_TIMEOUT must be finite and positive')
    return seconds


def wait_for_task(client, workflow, *, accept_confirmation=False):
    # Use the existing task-state browser budget. A fixed number of half-second
    # polls incorrectly expires while later native/DFM activities are healthy.
    deadline = time.monotonic() + task_wait_seconds()
    confirmed = False
    while True:
        snapshot = client.get(f'/api/tasks/{workflow}/snapshot').raise_for_status().json()
        if snapshot['status'] == 'waiting_confirmation' and accept_confirmation and not confirmed:
            client.post(f'/api/tasks/{workflow}/confirmation', json={'accepted': True}).raise_for_status()
            confirmed = True
        if snapshot['status'] in {'succeeded', 'failed', 'cancelled', 'timed_out'}:
            return snapshot
        if time.monotonic() >= deadline:
            raise AssertionError(f'Real workflow exceeded test deadline: {workflow}; '
                                 f'status={snapshot["status"]}; steps={snapshot.get("steps", [])}')
        time.sleep(.5)


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
