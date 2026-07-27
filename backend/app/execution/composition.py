"""Application composition for the replaceable execution boundary."""

from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.execution.backend import ExecutionBackend
from app.execution.podman_backend import DockerExecutionBackend, PodmanExecutionBackend


@lru_cache(maxsize=1)
def get_execution_backend() -> ExecutionBackend:
    """Return the process-wide execution backend selected by deployment config."""

    runtime = settings.sandbox_runtime.strip().lower()
    if runtime == "podman":
        configured = (settings.sandbox_command or "").strip()
        command = "podman" if configured in {"", "docker", "podman"} else configured
        return PodmanExecutionBackend(
            settings.sandbox_image,
            command=command,
        )
    if runtime == "docker":
        return DockerExecutionBackend(settings.sandbox_image)
    raise RuntimeError(
        f"Unsupported SANDBOX_RUNTIME '{settings.sandbox_runtime}'. Use docker or podman."
    )


@lru_cache(maxsize=1)
def get_compatibility_executor():
    """Return the legacy-shaped adapter backed by the shared execution backend."""

    from app.execution.compat_executor import CompatibilityExecutor

    return CompatibilityExecutor(get_execution_backend())
