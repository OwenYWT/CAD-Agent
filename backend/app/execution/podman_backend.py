"""Local OCI execution backend with normalized, auditable results."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from time import monotonic
from datetime import datetime, timezone
from pathlib import Path
from app.execution.canonical import sha256_file
from typing import Callable, Mapping, cast, get_args

from app.config import settings
from app.freecad.contracts import capability_contract_sha256
from app.execution.backend import MaterializedExecutionOutcome, RuntimeSnapshot
from app.execution.canonical import canonical_sha256
from app.execution.contracts import (
    ArtifactOutput,
    ExecutionError,
    ExecutionErrorCategory,
    ExecutionLeaseEnvelope,
    ExecutionResult,
    ExecutionSpec,
    ExecutionStatus,
    RuntimePlatform,
    RuntimeProvenance,
)
from app.sandbox.executor import CadQueryExecutor, SandboxResult


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
    platform = f"{inspected.get('Os')}/{inspected.get('Architecture')}"
    if platform not in get_args(RuntimePlatform):
        raise RuntimeError(f"unsupported runtime platform: {platform}")

    probe = subprocess.run(
        [
            command,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=64m,noexec,nosuid",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            "/opt/freecad/bin/FreeCADCmd",
            image_ref,
            "-c",
            (
                "exec(compile(open('/opt/cad-agent/freecad_runtime_probe.py', "
                "encoding='utf-8').read(), "
                "'/opt/cad-agent/freecad_runtime_probe.py', 'exec'))"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if probe.returncode != 0:
        raise RuntimeError(
            (probe.stderr or probe.stdout).strip() or "runtime version probe failed"
        )
    report = None
    for line in reversed(probe.stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            report = candidate
            break
    required_checks = {
        "freecad.create_save_reopen_modify",
        "freecad.transaction_abort",
        "freecad.sketch_solve",
        "freecad.shape_check",
        "freecad.export_step_stl",
        "freecad.assembly_bom",
    }
    if (
        report is None
        or report.get("schema_version") != "mcad-runtime-probe.v2"
        or set(report.get("verified_operations") or ()) != required_checks
        or any(
            check.get("status") != "passed"
            for check in report.get("checks") or ()
        )
    ):
        raise RuntimeError("runtime operation probe returned incomplete evidence")
    if report.get("freecad_capabilities_sha256") != capability_contract_sha256():
        raise RuntimeError("FreeCAD host/sandbox capability contracts differ; deploy matching backend and sandbox images")
    versions = report.get("versions")
    if not isinstance(versions, dict) or versions.get("freecad") != "1.1.4":
        raise RuntimeError("runtime operation probe returned wrong FreeCAD version")
    return RuntimeSnapshot(
        image_digest=_image_digest(inspected),
        platform=cast(RuntimePlatform, platform),
        versions={str(key): str(value) for key, value in versions.items()},
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


_STRUCTURED_ERROR_POLICY: dict[str, tuple[ExecutionErrorCategory, bool]] = {
    "object_not_found": (ExecutionErrorCategory.USER_CODE, False),
    "invalid_export": (ExecutionErrorCategory.USER_CODE, False),
    "object_name_conflict": (ExecutionErrorCategory.USER_CODE, False),
    "object_type_mismatch": (ExecutionErrorCategory.USER_CODE, False),
    "agent_plan_backend_mismatch": (ExecutionErrorCategory.VALIDATION, False),
    "invalid_edge_selection_mode": (ExecutionErrorCategory.USER_INPUT, False),
    "topology_resolution_failed": (ExecutionErrorCategory.VALIDATION, False),
    "topology_target_mismatch": (ExecutionErrorCategory.VALIDATION, False),
    "sketch_malformed_constraint": (ExecutionErrorCategory.USER_INPUT, False),
    "sketch_redundant_constraints": (ExecutionErrorCategory.VALIDATION, False),
    "sketch_conflicting_constraints": (ExecutionErrorCategory.VALIDATION, False),
    "sketch_under_constrained": (ExecutionErrorCategory.VALIDATION, False),
    "sketch_solver_failed": (ExecutionErrorCategory.CAD_KERNEL, False),
    "sketch_parameter_dependency_failed": (ExecutionErrorCategory.VALIDATION, False),
    "constraint_repair_verification_failed": (ExecutionErrorCategory.VALIDATION, False),
    "profile_geometry_invalid": (ExecutionErrorCategory.VALIDATION, False),
    "shape_check_failed": (ExecutionErrorCategory.CAD_KERNEL, False),
    "native_import_geometry_invalid": (ExecutionErrorCategory.VALIDATION, False),
    "native_import_precision_unsupported": (ExecutionErrorCategory.VALIDATION, False),
    "native_import_diagnostic_failed": (ExecutionErrorCategory.INFRASTRUCTURE, False),
    "invalid_document_object": (ExecutionErrorCategory.CAD_KERNEL, False),
    "parameter_state_stale": (ExecutionErrorCategory.VALIDATION, False),
    "parameter_state_missing": (ExecutionErrorCategory.VALIDATION, False),
    "parameter_duplicate_update": (ExecutionErrorCategory.USER_INPUT, False),
    "parameter_not_editable": (ExecutionErrorCategory.USER_INPUT, False),
    "parameter_value_type_invalid": (ExecutionErrorCategory.USER_INPUT, False),
    "parameter_property_type_changed": (ExecutionErrorCategory.VALIDATION, False),
    "parameter_value_out_of_range": (ExecutionErrorCategory.USER_INPUT, False),
    "subtractive_feature_no_effect": (ExecutionErrorCategory.VALIDATION, False),
    "bom_input_missing": (ExecutionErrorCategory.ARTIFACT, False),
    "bom_input_ambiguous": (ExecutionErrorCategory.ARTIFACT, False),
    "bom_input_integrity_failed": (ExecutionErrorCategory.ARTIFACT, False),
    "bom_runtime_unsupported": (ExecutionErrorCategory.INFRASTRUCTURE, False),
    "bom_source_not_assembly": (ExecutionErrorCategory.VALIDATION, False),
    "bom_source_hierarchy_lost": (ExecutionErrorCategory.VALIDATION, False),
    "bom_source_geometry_mismatch": (ExecutionErrorCategory.VALIDATION, False),
    "bom_generation_failed": (ExecutionErrorCategory.CAD_KERNEL, False),
    "bom_empty": (ExecutionErrorCategory.VALIDATION, False),
    "bom_seal_failed": (ExecutionErrorCategory.ARTIFACT, False),
    "bom_seal_timed_out": (ExecutionErrorCategory.TIMEOUT, False),
    "sandbox_protocol_error": (ExecutionErrorCategory.INFRASTRUCTURE, False),
    "freecad_internal_error": (ExecutionErrorCategory.INTERNAL, False),
    "api_execution_failed": (ExecutionErrorCategory.USER_CODE, False),
    "api_dependency_unavailable": (ExecutionErrorCategory.INFRASTRUCTURE, False),
    "api_runtime_unavailable": (ExecutionErrorCategory.INFRASTRUCTURE, False),
}


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
        self._runtime_failure: str | None = None
        self._probe_after = 0.0
        self._probe_lock = threading.Lock()

    def runtime_snapshot(self) -> RuntimeSnapshot:
        with self._probe_lock:
            if monotonic() < self._probe_after:
                if self._runtime_snapshot is not None:
                    return self._runtime_snapshot
                raise RuntimeError(self._runtime_failure)
            try:
                snapshot = self._runtime_inspector()
            except Exception as exc:
                self._runtime_snapshot = None
                self._runtime_failure = str(exc)
                self._probe_after = monotonic() + settings.dependency_readiness_timeout_s
                raise
            self._runtime_snapshot = snapshot
            self._runtime_failure = None
            self._probe_after = monotonic() + 60
            return snapshot

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
            snapshot = await asyncio.to_thread(self.runtime_snapshot)
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
            extra_files = await asyncio.to_thread(self._validated_inputs, spec, materialized_inputs or {})
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
            if sandbox_result.error_code:
                status = ExecutionStatus.FAILED
                code = sandbox_result.error_code
                category, retryable = _STRUCTURED_ERROR_POLICY.get(
                    code,
                    (ExecutionErrorCategory.INTERNAL, False),
                )
                structured = sandbox_result.error_details
            elif spec.capability == "mcad.freecad":
                status = ExecutionStatus.FAILED
                category = ExecutionErrorCategory.INFRASTRUCTURE
                code = "sandbox_protocol_error"
                retryable = False
                structured = {}
            else:
                status, category, code = _failure_mapping(sandbox_result)
                retryable = False
                structured = {}
            result = ExecutionResult(
                execution_attempt_id=spec.execution_attempt_id,
                status=status,
                error=ExecutionError(
                    category=category,
                    code=code,
                    message=_redact(sandbox_result.error_message or sandbox_result.error_type, redacted_values),
                    operation_id=(
                        str(structured.get("operation_id"))[:200]
                        if structured.get("operation_id") is not None
                        else None
                    ),
                    action=(
                        str(structured.get("action"))[:200]
                        if structured.get("action") is not None
                        else None
                    ),
                    details=dict(structured.get("details") or {}),
                    retryable=retryable,
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
            files, outputs = await asyncio.to_thread(self._verified_outputs, spec, sandbox_result.files)
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
            if path.stat().st_size != declaration.size_bytes:
                raise ValueError(f"input artifact size mismatch: {artifact_id}")
            if sha256_file(path) != declaration.sha256:
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
        roles = {item.name for item in spec.outputs}
        for declaration in spec.outputs:
            path = produced.get(declaration.name)
            if path is None:
                suffix = f".{declaration.name.lower()}"
                matches = {candidate for name, candidate in produced.items()
                           if name not in roles and (Path(name).suffix.lower() == suffix or candidate.suffix.lower() == suffix)}
                if len(matches) > 1:
                    raise ValueError(f"ambiguous output for declared role: {declaration.name}")
                path = next(iter(matches), None)
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
            digest = sha256_file(path)
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
