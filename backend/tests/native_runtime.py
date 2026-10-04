"""Run native test cases with the engine selected by the test environment."""
import os

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
