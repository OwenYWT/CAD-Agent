"""Run native test cases with the engine selected by the test environment."""
import os
import subprocess

from app.execution.podman_backend import DockerExecutionBackend, PodmanExecutionBackend
from app.sandbox.executor import CadQueryExecutor


def native_executor() -> CadQueryExecutor:
    runtime = os.environ.get("SANDBOX_RUNTIME", "podman").strip().lower()
    if runtime not in {"docker", "podman"}:
        raise ValueError("native tests require SANDBOX_RUNTIME=docker or podman")
    return CadQueryExecutor(
        runtime_name=runtime,
        image_ref=os.environ["SANDBOX_IMAGE"],
        sandbox_command=os.environ.get("SANDBOX_COMMAND", runtime),
    )


def native_backend() -> PodmanExecutionBackend:
    executor = native_executor()
    if executor.runtime == "docker":
        return DockerExecutionBackend(executor.image_ref, sandbox_executor=executor)
    return PodmanExecutionBackend(
        executor.image_ref,
        command=os.environ.get("SANDBOX_COMMAND", "podman"),
        sandbox_executor=executor,
    )


def native_execution_active(backend: PodmanExecutionBackend) -> bool:
    """Observe an actual running container on the configured native engine."""
    executor = backend._sandbox
    if executor.runtime == 'docker':
        with executor._active_lock:
            return bool(executor._active_containers)
    client = executor.client
    with client._active_lock:
        names = [name for name, _ in client._active.values()]
    # A live Podman CLI can precede container creation. Do not interrupt the
    # worker until the engine confirms that native execution has started.
    for name in names:
        state = subprocess.run([client.command, 'inspect', '--format', '{{.State.Running}}', name],
                               capture_output=True, text=True, timeout=5)
        if state.returncode == 0 and state.stdout.strip() == 'true':
            return True
    return False
