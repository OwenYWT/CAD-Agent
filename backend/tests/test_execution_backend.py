import hashlib
import os
from pathlib import Path

import pytest

from app.execution.backend import MaterializedExecutionOutcome
from app.execution.compat_executor import CompatibilityExecutor
from app.execution.contracts import (
    ExecutionSource,
    ExecutionSpec,
    ExecutionStatus,
    OutputDeclaration,
    ResourceLimits,
    RuntimeRequirement,
)
from app.execution.podman_backend import (
    PodmanExecutionBackend,
    RuntimeSnapshot,
    inspect_container_runtime,
)
from app.sandbox.executor import SandboxResult


IMAGE_DIGEST = f"sha256:{'b' * 64}"


def _spec(code: str = "result = cq.Workplane('XY').box(1, 1, 1)", **updates):
    values = {
        "execution_attempt_id": "attempt-backend-01",
        "workflow_run_id": "workflow-backend-01",
        "step_run_id": "step-backend-01",
        "tenant_id": "tenant-backend-01",
        "project_id": "project-backend-01",
        "idempotency_key": "workflow-backend-01:model:1",
        "capability": "mcad.cadquery",
        "operation": "generate",
        "mode": "3d",
        "source": ExecutionSource(
            language="python",
            code=code,
            sha256=hashlib.sha256(code.encode("utf-8")).hexdigest(),
        ),
        "outputs": (
            OutputDeclaration(name="step", media_type="model/step"),
            OutputDeclaration(name="stl", media_type="model/stl"),
        ),
        "runtime": RuntimeRequirement(
            image_digest=IMAGE_DIGEST,
            platform="linux/arm64",
        ),
        "limits": ResourceLimits(timeout_seconds=60),
    }
    values.update(updates)
    return ExecutionSpec(**values)


class RecordingSandboxExecutor:
    def __init__(self, result: SandboxResult):
        self.result = result
        self.calls = []

    async def execute(self, code, mode="3d", extra_files=None, timeout_s=None):
        self.calls.append((code, mode, extra_files or {}, timeout_s))
        return self.result


def _runtime_snapshot(digest: str = IMAGE_DIGEST):
    return RuntimeSnapshot(
        image_digest=digest,
        platform="linux/arm64",
        versions={"python": "3.11.15", "cadquery": "2.8.0", "ocp": "7.9.3.1"},
    )


def _successful_sandbox(tmp_path: Path):
    work_dir = tmp_path / "work"
    output_dir = work_dir / "output"
    output_dir.mkdir(parents=True)
    step = output_dir / "result.step"
    stl = output_dir / "result.stl"
    step.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
    stl.write_bytes(b"solid box\nendsolid box\n")
    return SandboxResult(
        success=True,
        files={"result.step": step, "result.stl": stl},
        error_type=None,
        error_message=None,
        traceback=None,
        execution_time_ms=17,
        work_dir=work_dir,
    )


@pytest.mark.asyncio
async def test_backend_materializes_verified_outputs_and_provenance(tmp_path):
    sandbox = RecordingSandboxExecutor(_successful_sandbox(tmp_path))
    backend = PodmanExecutionBackend(
        image_ref="cad-agent-sandbox:test",
        sandbox_executor=sandbox,
        runtime_inspector=lambda: _runtime_snapshot(),
    )

    outcome = await backend.execute(_spec())

    assert isinstance(outcome, MaterializedExecutionOutcome)
    assert outcome.result.status is ExecutionStatus.SUCCEEDED
    assert {item.name for item in outcome.result.outputs} == {"result.step", "result.stl"}
    assert set(outcome.files) == {"step", "stl"}
    assert outcome.result.provenance.image_digest == IMAGE_DIGEST
    assert outcome.result.provenance.code_hash == _spec().source.sha256
    assert sandbox.calls[0][0] == _spec().source.code


@pytest.mark.asyncio
async def test_backend_rejects_success_with_missing_required_artifact(tmp_path):
    result = _successful_sandbox(tmp_path)
    result.files.pop("result.step")
    backend = PodmanExecutionBackend(
        image_ref="cad-agent-sandbox:test",
        sandbox_executor=RecordingSandboxExecutor(result),
        runtime_inspector=lambda: _runtime_snapshot(),
    )

    outcome = await backend.execute(_spec())

    assert outcome.result.status is ExecutionStatus.ARTIFACT_REJECTED
    assert outcome.result.error.code == "missing_required_output"
    assert outcome.files == {}


@pytest.mark.asyncio
async def test_backend_normalizes_runtime_unavailable_without_leaking_secrets(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    sandbox = RecordingSandboxExecutor(
        SandboxResult(
            success=False,
            files={},
            error_type="SandboxUnavailable",
            error_message="socket failed; token=top-secret",
            traceback=None,
            execution_time_ms=1,
            work_dir=work_dir,
        )
    )
    backend = PodmanExecutionBackend(
        image_ref="cad-agent-sandbox:test",
        sandbox_executor=sandbox,
        runtime_inspector=lambda: _runtime_snapshot(),
    )

    outcome = await backend.execute(_spec(), redacted_values=("top-secret",))
    serialized = outcome.result.model_dump_json()

    assert outcome.result.status is ExecutionStatus.FAILED
    assert outcome.result.error.category.value == "infrastructure"
    assert "top-secret" not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.asyncio
async def test_backend_rejects_runtime_digest_mismatch_before_execution(tmp_path):
    sandbox = RecordingSandboxExecutor(_successful_sandbox(tmp_path))
    backend = PodmanExecutionBackend(
        image_ref="cad-agent-sandbox:test",
        sandbox_executor=sandbox,
        runtime_inspector=lambda: _runtime_snapshot(f"sha256:{'c' * 64}"),
    )

    outcome = await backend.execute(_spec())

    assert outcome.result.status is ExecutionStatus.FAILED
    assert outcome.result.error.code == "runtime_digest_mismatch"
    assert sandbox.calls == []


@pytest.mark.asyncio
async def test_compatibility_executor_preserves_sandbox_result_shape(tmp_path):
    backend = PodmanExecutionBackend(
        image_ref="cad-agent-sandbox:test",
        sandbox_executor=RecordingSandboxExecutor(_successful_sandbox(tmp_path)),
        runtime_inspector=lambda: _runtime_snapshot(),
    )

    result = await CompatibilityExecutor(backend).execute(_spec().source.code)

    assert result.success is True
    assert set(result.files) == {"result.step", "result.stl"}
    assert result.error_type is None
    assert result.work_dir == tmp_path / "work"


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_REAL_PODMAN") != "1",
    reason="set RUN_REAL_PODMAN=1 to exercise the actual sandbox image",
)
async def test_real_podman_backend_exports_step_and_stl():
    image_ref = os.environ["SANDBOX_IMAGE"]
    snapshot = inspect_container_runtime("podman", image_ref)
    spec = _spec(
        runtime=RuntimeRequirement(
            image_digest=snapshot.image_digest,
            platform=snapshot.platform,
        )
    )
    backend = PodmanExecutionBackend(image_ref=image_ref)

    outcome = await backend.execute(spec)

    assert outcome.result.status is ExecutionStatus.SUCCEEDED
    assert set(outcome.files) == {"step", "stl"}
    assert all(path.exists() and path.stat().st_size > 0 for path in outcome.files.values())
