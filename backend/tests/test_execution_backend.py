import hashlib
import json
import os
from pathlib import Path

import pytest

from app.execution.backend import MaterializedExecutionOutcome
from app.execution.capability_adapter import CapabilityExecutionAdapter
from app.execution.compat_executor import (
    CompatibilityExecutor,
    _normalized_error_type,
)
from app.execution.contracts import (
    ArtifactInput,
    ExecutionError,
    ExecutionErrorCategory,
    ExecutionResult,
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


@pytest.mark.parametrize(
    ("status", "category", "runtime_type", "expected"),
    [
        (ExecutionStatus.FAILED, ExecutionErrorCategory.INFRASTRUCTURE, "SandboxUnavailable", "SandboxUnavailable"),
        (ExecutionStatus.FAILED, ExecutionErrorCategory.INFRASTRUCTURE, "DockerError", "ContainerLaunchError"),
        (ExecutionStatus.TIMED_OUT, ExecutionErrorCategory.TIMEOUT, "TimeoutError", "ExecutionTimeout"),
        (ExecutionStatus.OOM, ExecutionErrorCategory.RESOURCE, "MemoryError", "ExecutionOOM"),
        (ExecutionStatus.FAILED, ExecutionErrorCategory.USER_CODE, "SyntaxError", "InvalidCode"),
        (ExecutionStatus.FAILED, ExecutionErrorCategory.CAD_KERNEL, "StandardFailure", "CADKernelError"),
        (ExecutionStatus.ARTIFACT_REJECTED, ExecutionErrorCategory.ARTIFACT, None, "ArtifactRejected"),
        (ExecutionStatus.CANCELLED, ExecutionErrorCategory.CANCELLATION, None, "ExecutionCancelled"),
    ],
)
def test_compatibility_boundary_normalizes_execution_failures(
    status,
    category,
    runtime_type,
    expected,
):
    evidence = {"runtime_error_type": runtime_type} if runtime_type else {}
    result = ExecutionResult(
        execution_attempt_id="attempt-normalized-error",
        status=status,
        error=ExecutionError(
            category=category,
            code="normalized_test",
            message="failure",
            evidence=evidence,
        ),
    )

    assert _normalized_error_type(result) == expected


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


def _freecad_spec():
    task = {
        "schema_version": "mcad-capability-task.v1",
        "capability": "freecad",
        "operation": "execute",
        "params": {},
        "inputs": {},
    }
    code = json.dumps(task, sort_keys=True)
    return _spec(
        code,
        capability="mcad.freecad",
        operation="execute",
        source=ExecutionSource(
            language="json",
            code=code,
            sha256=hashlib.sha256(code.encode()).hexdigest(),
        ),
        outputs=(),
    )


class RecordingSandboxExecutor:
    def __init__(self, result: SandboxResult):
        self.result = result
        self.calls = []

    async def execute(
        self,
        code,
        mode="3d",
        extra_files=None,
        timeout_s=None,
        task=None,
        resource_limits=None,
    ):
        self.calls.append((code, mode, extra_files or {}, timeout_s, task))
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
async def test_compatibility_executor_normalizes_runtime_probe_failure(tmp_path):
    class UnavailableBackend:
        def runtime_snapshot(self):
            raise RuntimeError("podman socket secret-detail unavailable")

        async def execute(self, *_args, **_kwargs):
            pytest.fail("execution must not be submitted without a runtime snapshot")

    result = await CompatibilityExecutor(UnavailableBackend()).execute(
        "result = cq.Workplane('XY').box(1, 1, 1)"
    )

    assert result.success is False
    assert result.error_type == "SandboxUnavailable"
    assert "MCAD execution runtime is unavailable" in result.error_message
    assert result.work_dir.is_dir()


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
@pytest.mark.parametrize("error_code,category", [
    ("sketch_conflicting_constraints", ExecutionErrorCategory.VALIDATION),
    ("shape_check_failed", ExecutionErrorCategory.CAD_KERNEL),
    ("invalid_document_object", ExecutionErrorCategory.CAD_KERNEL),
    ("freecad_internal_error", ExecutionErrorCategory.INTERNAL),
])
async def test_backend_preserves_structured_freecad_error(tmp_path, error_code, category):
    sandbox = RecordingSandboxExecutor(SandboxResult(
        success=False,
        files={},
        error_type="StructuredExecutionError",
        error_message="Sketch solver reported conflicting constraints",
        traceback="internal traceback must not be projected",
        execution_time_ms=9,
        work_dir=tmp_path,
        error_code=error_code,
        error_details={
            "schema_version": "mcad-error.v1",
            "code": error_code,
            "message": "Sketch solver reported conflicting constraints",
            "operation_id": "constraint-04",
            "action": "sketch.add_constraint",
            "details": {"object": "Sketch", "solver_status": -3},
        },
    ))
    backend = PodmanExecutionBackend(
        image_ref="cad-agent-sandbox:test",
        sandbox_executor=sandbox,
        runtime_inspector=lambda: _runtime_snapshot(),
    )

    outcome = await backend.execute(_freecad_spec())

    assert outcome.result.error.code == error_code
    assert outcome.result.error.category is category
    assert outcome.result.error.operation_id == "constraint-04"
    assert outcome.result.error.action == "sketch.add_constraint"
    assert outcome.result.error.details["solver_status"] == -3
    assert outcome.result.error.retryable is False
    assert "traceback" not in outcome.result.error.model_dump(mode="json")


@pytest.mark.asyncio
async def test_freecad_failure_without_structured_envelope_is_protocol_error(tmp_path):
    sandbox = RecordingSandboxExecutor(SandboxResult(
        success=False,
        files={},
        error_type="RuntimeError",
        error_message="unstructured failure",
        traceback=None,
        execution_time_ms=2,
        work_dir=tmp_path,
    ))
    backend = PodmanExecutionBackend(
        image_ref="cad-agent-sandbox:test",
        sandbox_executor=sandbox,
        runtime_inspector=lambda: _runtime_snapshot(),
    )

    outcome = await backend.execute(_freecad_spec())

    assert outcome.result.error.code == "sandbox_protocol_error"
    assert outcome.result.error.category is ExecutionErrorCategory.INFRASTRUCTURE


@pytest.mark.asyncio
async def test_backend_forwards_only_typed_json_capability_task(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    artifact = work / "inspect.json"
    artifact.write_text('{"solids":1}', encoding="utf-8")
    metadata = work / "capability-result.json"
    metadata.write_text('{"exit_code":0}', encoding="utf-8")
    sandbox = RecordingSandboxExecutor(
        SandboxResult(
            success=True,
            files={"artifact": artifact, "capability-result": metadata},
            error_type=None,
            error_message=None,
            traceback=None,
            execution_time_ms=4,
            work_dir=work,
        )
    )
    backend = PodmanExecutionBackend(
        image_ref="cad-agent-sandbox:test",
        sandbox_executor=sandbox,
        runtime_inspector=lambda: _runtime_snapshot(),
    )
    task = {
        "schema_version": "mcad-capability-task.v1",
        "capability": "cad",
        "operation": "inspect",
        "params": {"operation": "refs", "output": "inspect.json"},
        "inputs": {"input": "input-part.step"},
    }
    source_code = json.dumps(task, sort_keys=True, separators=(",", ":"))
    spec = _spec(
        capability="mcad.cad",
        operation="inspect",
        mode="analysis",
        source=ExecutionSource(
            language="json",
            code=source_code,
            sha256=hashlib.sha256(source_code.encode()).hexdigest(),
        ),
        outputs=(
            OutputDeclaration(name="artifact", media_type="application/json"),
            OutputDeclaration(name="capability-result", media_type="application/json"),
        ),
    )

    outcome = await backend.execute(spec)

    assert outcome.result.status is ExecutionStatus.SUCCEEDED
    assert sandbox.calls[0][4] == task


@pytest.mark.asyncio
async def test_capability_adapter_forwards_revision_fence_and_typed_multi_outputs(
    tmp_path,
):
    class RecordingBackend:
        def __init__(self):
            self.spec = None

        def runtime_snapshot(self):
            return _runtime_snapshot()

        async def execute(self, spec, **_kwargs):
            self.spec = spec
            return MaterializedExecutionOutcome(
                result=ExecutionResult(
                    execution_attempt_id=spec.execution_attempt_id,
                    status=ExecutionStatus.SUCCEEDED,
                ),
                files={},
                work_dir=tmp_path,
            )

    backend = RecordingBackend()
    adapter = CapabilityExecutionAdapter(backend)

    await adapter.execute(
        capability="freecad",
        operation="execute",
        request_id="freecad-adapter",
        params={"plan": {"schema_version": "freecad-operation-plan.v1"}},
        inputs={},
        artifact_media_type="application/octet-stream",
        mode="3d",
        timeout_seconds=120,
        output_bytes=64 * 1024 * 1024,
        expected_base_revision_id="revision-base-1",
        declared_outputs={
            "fcstd": "application/vnd.freecad.fcstd",
            "state": "application/json",
            "step": "model/step",
        },
    )

    assert backend.spec.expected_base_revision_id == "revision-base-1"
    assert {output.name for output in backend.spec.outputs} == {
        "fcstd",
        "state",
        "step",
        "capability-result",
    }


@pytest.mark.asyncio
async def test_backend_rejects_traversal_in_materialized_input_filename(tmp_path):
    source = tmp_path / "part.step"
    source.write_bytes(b"STEP")
    declaration = ArtifactInput(
        artifact_id="input-traversal",
        filename="../part.step",
        sha256=hashlib.sha256(b"STEP").hexdigest(),
        size_bytes=4,
        media_type="model/step",
    )
    sandbox = RecordingSandboxExecutor(_successful_sandbox(tmp_path))
    backend = PodmanExecutionBackend(
        image_ref="cad-agent-sandbox:test",
        sandbox_executor=sandbox,
        runtime_inspector=lambda: _runtime_snapshot(),
    )

    outcome = await backend.execute(
        _spec(inputs=(declaration,)),
        materialized_inputs={"input-traversal": source},
    )

    assert outcome.result.status is ExecutionStatus.ARTIFACT_REJECTED
    assert "filename" in outcome.result.error.message
    assert sandbox.calls == []


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
