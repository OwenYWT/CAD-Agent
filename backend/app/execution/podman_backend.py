"""Local OCI execution backend with normalized, auditable results."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from app.execution.backend import MaterializedExecutionOutcome
from app.execution.canonical import canonical_sha256
from app.execution.contracts import (
    ArtifactOutput,
    ExecutionError,
    ExecutionErrorCategory,
    ExecutionLeaseEnvelope,
    ExecutionResult,
    ExecutionSpec,
    ExecutionStatus,
    RuntimeProvenance,
)
from app.sandbox.executor import CadQueryExecutor, SandboxResult


@dataclass(frozen=True)
class RuntimeSnapshot:
    image_digest: str
    platform: str
    versions: dict[str, str]


def _image_digest(inspected: dict) -> str:
    image_id = str(inspected.get("Id") or inspected.get("ID") or "")
    if image_id and not image_id.startswith("sha256:"):
        image_id = f"sha256:{image_id}"
    if not image_id:
        raise RuntimeError("container image inspect returned no immutable image ID")
    return image_id


def inspect_container_runtime(command: str, image_ref: str) -> RuntimeSnapshot:
    inspected_process = subprocess.run(
        [command, "image", "inspect", image_ref],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if inspected_process.returncode != 0:
        raise RuntimeError(
            (inspected_process.stderr or inspected_process.stdout).strip()
            or f"{command} could not inspect {image_ref}"
        )
    inspected = json.loads(inspected_process.stdout)[0]
    architecture = inspected.get("Architecture")
    operating_system = inspected.get("Os")
    if architecture not in {"amd64", "arm64"} or operating_system != "linux":
        raise RuntimeError(
            f"unsupported runtime platform: {operating_system}/{architecture}"
        )

    probe_code = (
        "import json,sys\n"
        "versions={'python':sys.version.split()[0]}\n"
        "for module_name in ('cadquery','build123d','OCP','ezdxf'):\n"
        "  try:\n"
        "    module=__import__(module_name)\n"
        "    version=getattr(module,'__version__',None) or getattr(module,'VERSION',None)\n"
        "    if version is not None: versions[module_name.lower()]=str(version)\n"
        "  except Exception: pass\n"
        "print(json.dumps(versions,sort_keys=True))\n"
    )
    probe = subprocess.run(
        [
            command,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            "python",
            image_ref,
            "-c",
            probe_code,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0:
        raise RuntimeError(
            (probe.stderr or probe.stdout).strip() or "runtime version probe failed"
        )
    return RuntimeSnapshot(
        image_digest=_image_digest(inspected),
        platform=f"linux/{architecture}",
        versions=json.loads(probe.stdout),
    )


def _redact(message: str | None, values: tuple[str, ...]) -> str:
    redacted = message or "Execution failed"
    for value in values:
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted[:4000]


def _failure_mapping(result: SandboxResult) -> tuple[ExecutionStatus, ExecutionErrorCategory, str]:
    error_type = result.error_type or "ExecutionError"
    normalized_error_type = error_type.lower()
    if error_type in {"TimeoutError", "APITimeoutError"}:
        return ExecutionStatus.TIMED_OUT, ExecutionErrorCategory.TIMEOUT, "execution_timeout"
    if (
        error_type in {"OOMError", "OutOfMemoryError", "MemoryError"}
        or ("memory" in normalized_error_type and "error" in normalized_error_type)
    ):
        return ExecutionStatus.OOM, ExecutionErrorCategory.RESOURCE, "execution_oom"
    if error_type in {
        "SandboxUnavailable",
        "DockerError",
        "PodmanError",
        "ContainerLaunchError",
    }:
        return ExecutionStatus.FAILED, ExecutionErrorCategory.INFRASTRUCTURE, "runtime_unavailable"
    if error_type in {"SyntaxError", "ImportError", "NameError", "TypeError", "ValueError"}:
        return ExecutionStatus.FAILED, ExecutionErrorCategory.USER_CODE, "user_code_failed"
    return ExecutionStatus.FAILED, ExecutionErrorCategory.CAD_KERNEL, "cad_execution_failed"


class PodmanExecutionBackend:
    def __init__(
        self,
        image_ref: str,
        *,
        command: str = "podman",
        sandbox_executor=None,
        runtime_inspector: Callable[[], RuntimeSnapshot] | None = None,
    ):
        self.image_ref = image_ref
        self.command = command
        self._sandbox = sandbox_executor or CadQueryExecutor(
            runtime_name="podman",
            image_ref=image_ref,
            sandbox_command=command,
        )
        self._runtime_inspector = runtime_inspector or (
            lambda: inspect_container_runtime(command, image_ref)
        )
        self._runtime_snapshot: RuntimeSnapshot | None = None

    def runtime_snapshot(self) -> RuntimeSnapshot:
        if self._runtime_snapshot is None:
            self._runtime_snapshot = self._runtime_inspector()
        return self._runtime_snapshot

    async def execute(
        self,
        spec: ExecutionSpec,
        lease: ExecutionLeaseEnvelope | None = None,
        materialized_inputs: Mapping[str, Path] | None = None,
        redacted_values: tuple[str, ...] = (),
    ) -> MaterializedExecutionOutcome:
        del lease  # local delivery has no remote transport envelope
        started_at = datetime.now(timezone.utc)
        try:
            snapshot = self.runtime_snapshot()
        except Exception as exc:
            result = self._control_failure(
                spec,
                started_at,
                code="runtime_inspection_failed",
                message=_redact(str(exc), redacted_values),
            )
            return MaterializedExecutionOutcome(result=result, files={}, work_dir=None)

        if snapshot.image_digest != spec.runtime.image_digest:
            result = self._control_failure(
                spec,
                started_at,
                code="runtime_digest_mismatch",
                message=(
                    f"ExecutionSpec requires {spec.runtime.image_digest}, "
                    f"but {self.image_ref} resolved to {snapshot.image_digest}"
                ),
                snapshot=snapshot,
            )
            return MaterializedExecutionOutcome(result=result, files={}, work_dir=None)
        if snapshot.platform != spec.runtime.platform:
            result = self._control_failure(
                spec,
                started_at,
                code="runtime_platform_mismatch",
                message=(
                    f"ExecutionSpec requires {spec.runtime.platform}, "
                    f"but runtime is {snapshot.platform}"
                ),
                snapshot=snapshot,
            )
            return MaterializedExecutionOutcome(result=result, files={}, work_dir=None)

        try:
            task = self._validated_task(spec)
        except Exception as exc:
            result = ExecutionResult(
                execution_attempt_id=spec.execution_attempt_id,
                status=ExecutionStatus.FAILED,
                error=ExecutionError(
                    category=ExecutionErrorCategory.USER_INPUT,
                    code="invalid_execution_task",
                    message=_redact(str(exc), redacted_values),
                ),
                provenance=self._provenance(spec, snapshot),
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )
            return MaterializedExecutionOutcome(result=result, files={}, work_dir=None)

        try:
            extra_files = self._validated_inputs(spec, materialized_inputs or {})
        except Exception as exc:
            result = ExecutionResult(
                execution_attempt_id=spec.execution_attempt_id,
                status=ExecutionStatus.ARTIFACT_REJECTED,
                error=ExecutionError(
                    category=ExecutionErrorCategory.ARTIFACT,
                    code="input_artifact_rejected",
                    message=_redact(str(exc), redacted_values),
                ),
                provenance=self._provenance(spec, snapshot),
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )
            return MaterializedExecutionOutcome(result=result, files={}, work_dir=None)

        execute_kwargs = {
            "mode": spec.mode,
            "extra_files": extra_files,
            "timeout_s": spec.limits.timeout_seconds,
            "resource_limits": spec.limits,
        }
        if task is not None:
            execute_kwargs["task"] = task
        sandbox_result = await self._sandbox.execute(
            spec.source.code,
            **execute_kwargs,
        )
        if not sandbox_result.success:
            status, category, code = _failure_mapping(sandbox_result)
            result = ExecutionResult(
                execution_attempt_id=spec.execution_attempt_id,
                status=status,
                error=ExecutionError(
                    category=category,
                    code=code,
                    message=_redact(sandbox_result.error_message, redacted_values),
                    evidence={"runtime_error_type": sandbox_result.error_type},
                ),
                provenance=self._provenance(spec, snapshot),
                metrics={"execution_time_ms": sandbox_result.execution_time_ms},
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )
            return MaterializedExecutionOutcome(
                result=result,
                files={},
                work_dir=sandbox_result.work_dir,
            )

        try:
            files, outputs = self._verified_outputs(spec, sandbox_result.files)
        except Exception as exc:
            result = ExecutionResult(
                execution_attempt_id=spec.execution_attempt_id,
                status=ExecutionStatus.ARTIFACT_REJECTED,
                error=ExecutionError(
                    category=ExecutionErrorCategory.ARTIFACT,
                    code="missing_required_output",
                    message=_redact(str(exc), redacted_values),
                ),
                provenance=self._provenance(spec, snapshot),
                metrics={"execution_time_ms": sandbox_result.execution_time_ms},
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )
            return MaterializedExecutionOutcome(
                result=result,
                files={},
                work_dir=sandbox_result.work_dir,
            )

        result = ExecutionResult(
            execution_attempt_id=spec.execution_attempt_id,
            status=ExecutionStatus.SUCCEEDED,
            outputs=tuple(outputs),
            provenance=self._provenance(spec, snapshot),
            metrics={"execution_time_ms": sandbox_result.execution_time_ms},
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
        )
        return MaterializedExecutionOutcome(
            result=result,
            files=files,
            work_dir=sandbox_result.work_dir,
        )

    def _control_failure(
        self,
        spec: ExecutionSpec,
        started_at: datetime,
        *,
        code: str,
        message: str,
        snapshot: RuntimeSnapshot | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            execution_attempt_id=spec.execution_attempt_id,
            status=ExecutionStatus.FAILED,
            error=ExecutionError(
                category=ExecutionErrorCategory.INFRASTRUCTURE,
                code=code,
                message=message,
            ),
            provenance=self._provenance(spec, snapshot) if snapshot else None,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _validated_task(spec: ExecutionSpec) -> dict | None:
        if spec.source.language != "json":
            return None
        parsed = json.loads(spec.source.code)
        if not isinstance(parsed, dict):
            raise ValueError("JSON execution source must be an object")
        if parsed.get("schema_version") != "mcad-capability-task.v1":
            raise ValueError("unsupported MCAD capability task schema")
        capability = parsed.get("capability")
        operation = parsed.get("operation")
        if spec.capability != f"mcad.{capability}" or spec.operation != operation:
            raise ValueError("MCAD capability task does not match ExecutionSpec")
        if not isinstance(parsed.get("params"), dict) or not isinstance(
            parsed.get("inputs"),
            dict,
        ):
            raise ValueError("MCAD capability task params and inputs must be objects")
        return parsed

    @staticmethod
    def _validated_inputs(
        spec: ExecutionSpec,
        materialized: Mapping[str, Path],
    ) -> dict[str, Path]:
        by_artifact = {item.artifact_id: item for item in spec.inputs}
        if set(materialized) != set(by_artifact):
            raise ValueError("materialized input IDs do not match ExecutionSpec inputs")
        extra_files: dict[str, Path] = {}
        for artifact_id, path in materialized.items():
            declaration = by_artifact[artifact_id]
            filename = declaration.filename
            if (
                Path(filename).name != filename
                or filename.startswith(("-", "."))
                or "/" in filename
                or "\\" in filename
            ):
                raise ValueError(
                    f"input artifact filename is not a safe basename: {artifact_id}"
                )
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"input artifact is not a regular file: {artifact_id}")
            data = path.read_bytes()
            if len(data) != declaration.size_bytes:
                raise ValueError(f"input artifact size mismatch: {artifact_id}")
            if hashlib.sha256(data).hexdigest() != declaration.sha256:
                raise ValueError(f"input artifact hash mismatch: {artifact_id}")
            extra_files[filename] = path
        return extra_files

    @staticmethod
    def _verified_outputs(
        spec: ExecutionSpec,
        produced: Mapping[str, Path],
    ) -> tuple[dict[str, Path], list[ArtifactOutput]]:
        normalized: dict[str, Path] = {}
        outputs: list[ArtifactOutput] = []
        for declaration in spec.outputs:
            path = next(
                (
                    candidate
                    for name, candidate in produced.items()
                    if name == declaration.name
                    or Path(name).suffix.lower() == f".{declaration.name.lower()}"
                    or candidate.suffix.lower() == f".{declaration.name.lower()}"
                ),
                None,
            )
            if path is None:
                if declaration.required:
                    raise ValueError(f"required output was not produced: {declaration.name}")
                continue
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"output is not a regular file: {declaration.name}")
            size = path.stat().st_size
            if size <= 0:
                raise ValueError(f"output is empty: {declaration.name}")
            if declaration.max_size_bytes is not None and size > declaration.max_size_bytes:
                raise ValueError(f"output exceeds declared size: {declaration.name}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            normalized[declaration.name] = path
            outputs.append(
                ArtifactOutput(
                    upload_id=f"{spec.execution_attempt_id}:{declaration.name}",
                    name=path.name,
                    sha256=digest,
                    size_bytes=size,
                    media_type=declaration.media_type,
                )
            )
        return normalized, outputs

    @staticmethod
    def _provenance(spec: ExecutionSpec, snapshot: RuntimeSnapshot) -> RuntimeProvenance:
        return RuntimeProvenance(
            image_digest=snapshot.image_digest,
            platform=snapshot.platform,
            versions=snapshot.versions,
            input_hash=canonical_sha256(spec.inputs),
            code_hash=spec.source.sha256,
            sandbox_tier=spec.runtime.sandbox_tier,
        )


class DockerExecutionBackend(PodmanExecutionBackend):
    """Compatibility backend for existing trusted-host Docker deployments."""

    def __init__(
        self,
        image_ref: str,
        *,
        sandbox_executor=None,
        runtime_inspector: Callable[[], RuntimeSnapshot] | None = None,
    ):
        super().__init__(
            image_ref,
            command="docker",
            sandbox_executor=sandbox_executor
            or CadQueryExecutor(runtime_name="docker", image_ref=image_ref),
            runtime_inspector=runtime_inspector
            or (lambda: inspect_container_runtime("docker", image_ref)),
        )
