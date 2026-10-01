"""Adapter preserving the current SandboxResult API during the M0 cutover."""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.execution.backend import ExecutionBackend
from app.execution.contracts import (
    ArtifactInput,
    ExecutionErrorCategory,
    ExecutionResult,
    ExecutionSource,
    ExecutionSpec,
    ExecutionStatus,
    OutputDeclaration,
    ResourceLimits,
    RuntimeRequirement,
)
from app.sandbox.executor import SandboxResult


@dataclass(frozen=True)
class CompatibilityExecutionContext:
    workflow_run_id: str
    step_run_id: str
    tenant_id: str
    project_id: str
    expected_base_revision_id: str | None = None


_context: ContextVar[CompatibilityExecutionContext | None] = ContextVar(
    "execution_compatibility_context",
    default=None,
)


@contextmanager
def bind_execution_context(context: CompatibilityExecutionContext):
    token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(token)


def _default_backend():
    from app.execution.composition import get_execution_backend

    return get_execution_backend()


def _output_declarations(mode: str) -> tuple[OutputDeclaration, ...]:
    if mode == "2d":
        return (OutputDeclaration(name="dxf", media_type="image/vnd.dxf"),)
    if mode == "analysis":
        return (OutputDeclaration(name="json", media_type="application/json"),)
    return (
        OutputDeclaration(name="step", media_type="model/step"),
        OutputDeclaration(name="stl", media_type="model/stl"),
    )


def _normalized_error_type(result: ExecutionResult) -> str:
    error = result.error
    runtime_error_type = error.evidence.get("runtime_error_type") if error else None
    if result.status is ExecutionStatus.TIMED_OUT:
        return "ExecutionTimeout"
    if result.status is ExecutionStatus.OOM:
        return "ExecutionOOM"
    if result.status is ExecutionStatus.CANCELLED:
        return "ExecutionCancelled"
    if result.status is ExecutionStatus.ARTIFACT_REJECTED:
        return "ArtifactRejected"
    if result.status is ExecutionStatus.LEASE_LOST:
        return "SandboxUnavailable"
    if error is None:
        return "ExecutionError"
    if error.category is ExecutionErrorCategory.INFRASTRUCTURE:
        if runtime_error_type in {
            "ContainerLaunchError",
            "DockerError",
            "PodmanError",
        }:
            return "ContainerLaunchError"
        return "SandboxUnavailable"
    if error.category in {
        ExecutionErrorCategory.USER_INPUT,
        ExecutionErrorCategory.USER_CODE,
        ExecutionErrorCategory.VALIDATION,
    }:
        return "InvalidCode"
    if error.category is ExecutionErrorCategory.CAD_KERNEL:
        return "CADKernelError"
    if error.category is ExecutionErrorCategory.ARTIFACT:
        return "ArtifactRejected"
    if error.category is ExecutionErrorCategory.CANCELLATION:
        return "ExecutionCancelled"
    if error.category is ExecutionErrorCategory.TIMEOUT:
        return "ExecutionTimeout"
    if error.category is ExecutionErrorCategory.RESOURCE:
        return "ExecutionOOM"
    return str(runtime_error_type or error.code or "ExecutionError")


class CompatibilityExecutor:
    def __init__(self, backend: ExecutionBackend | None = None):
        self.backend = backend or _default_backend()

    async def execute(
        self,
        code: str,
        mode: str = "3d",
        extra_files: dict[str, Path] | None = None,
    ) -> SandboxResult:
        try:
            snapshot = self.backend.runtime_snapshot()
        except Exception:
            work_dir = Path(tempfile.mkdtemp(prefix="cad_compat_"))
            return SandboxResult(
                success=False,
                files={},
                error_type="SandboxUnavailable",
                error_message="MCAD execution runtime is unavailable",
                traceback=None,
                execution_time_ms=0,
                work_dir=work_dir,
            )
        context = _context.get()
        local_id = str(uuid.uuid4())
        if context is None:
            context = CompatibilityExecutionContext(
                workflow_run_id=f"m0-workflow-{local_id}",
                step_run_id=f"m0-step-{local_id}",
                tenant_id=f"m0-tenant-{local_id}",
                project_id=f"m0-project-{local_id}",
            )

        materialized_inputs: dict[str, Path] = {}
        inputs = []
        for filename, path in (extra_files or {}).items():
            data = path.read_bytes()
            artifact_id = f"input-{uuid.uuid4()}"
            materialized_inputs[artifact_id] = path
            inputs.append(
                ArtifactInput(
                    artifact_id=artifact_id,
                    filename=filename,
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                    media_type="application/octet-stream",
                )
            )

        source = ExecutionSource(
            language="python",
            code=code,
            sha256=hashlib.sha256(code.encode("utf-8")).hexdigest(),
        )
        spec = ExecutionSpec(
            execution_attempt_id=f"m0-attempt-{local_id}",
            workflow_run_id=context.workflow_run_id,
            step_run_id=context.step_run_id,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            expected_base_revision_id=context.expected_base_revision_id,
            idempotency_key=f"{context.workflow_run_id}:{context.step_run_id}:{source.sha256}",
            capability="mcad.local",
            operation="execute",
            mode=mode,
            source=source,
            inputs=tuple(inputs),
            outputs=_output_declarations(mode),
            runtime=RuntimeRequirement(
                image_digest=snapshot.image_digest,
                platform=snapshot.platform,
            ),
            limits=ResourceLimits.from_configured_memory(settings.sandbox_memory_limit, timeout_seconds=settings.sandbox_timeout_s),
        )
        outcome = await self.backend.execute(
            spec,
            materialized_inputs=materialized_inputs,
        )
        execution_time = int(outcome.result.metrics.get("execution_time_ms") or 0)
        work_dir = outcome.work_dir or Path(tempfile.mkdtemp(prefix="cad_compat_"))
        if outcome.result.status is ExecutionStatus.SUCCEEDED:
            return SandboxResult(
                success=True,
                files={path.name: path for path in outcome.files.values()},
                error_type=None,
                error_message=None,
                traceback=None,
                execution_time_ms=execution_time,
                work_dir=work_dir,
            )

        error = outcome.result.error
        return SandboxResult(
            success=False,
            files={},
            error_type=_normalized_error_type(outcome.result),
            error_message=error.message if error else "Execution failed",
            traceback=None,
            execution_time_ms=execution_time,
            work_dir=work_dir,
        )
