"""Typed adapter from local MCAD capability actions to ``ExecutionBackend``."""

from __future__ import annotations
from app.config import settings

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.execution.backend import ExecutionBackend, MaterializedExecutionOutcome
from app.execution.contracts import (
    ArtifactInput,
    ExecutionSource,
    ExecutionSpec,
    OutputDeclaration,
    ResourceLimits,
    RuntimeRequirement,
)


@dataclass(frozen=True)
class CapabilityExecutionOutcome:
    execution: MaterializedExecutionOutcome
    metadata: dict[str, Any]


class CapabilityExecutionAdapter:
    def __init__(
        self,
        backend: ExecutionBackend,
        *,
        tenant_id: str = "local-tenant",
        project_id: str = "local-capability-workspace",
    ) -> None:
        self.backend = backend
        self.tenant_id = tenant_id
        self.project_id = project_id

    async def execute(
        self,
        *,
        capability: str,
        operation: str,
        request_id: str,
        params: Mapping[str, Any],
        inputs: Mapping[str, Path],
        artifact_media_type: str,
        mode: str,
        timeout_seconds: int,
        output_bytes: int,
        expected_base_revision_id: str | None = None,
        declared_outputs: Mapping[str, str] | None = None,
    ) -> CapabilityExecutionOutcome:
        snapshot = self.backend.runtime_snapshot()
        materialized: dict[str, Path] = {}
        declarations: list[ArtifactInput] = []
        task_inputs: dict[str, str] = {}
        for role, path in sorted(inputs.items()):
            data = path.read_bytes()
            artifact_id = f"input-{uuid.uuid4()}"
            filename = f"{role}-{path.name}"
            materialized[artifact_id] = path
            task_inputs[role] = filename
            declarations.append(
                ArtifactInput(
                    artifact_id=artifact_id,
                    filename=filename,
                    sha256=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                    media_type="application/octet-stream",
                )
            )

        task = {
            "schema_version": "mcad-capability-task.v1",
            "capability": capability,
            "operation": operation,
            "params": dict(params),
            "inputs": task_inputs,
        }
        code = json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        source = ExecutionSource(
            language="json",
            code=code,
            sha256=hashlib.sha256(code.encode("utf-8")).hexdigest(),
        )
        attempt_id = f"capability-{uuid.uuid4()}"
        output_declarations = [
            OutputDeclaration(
                name=name,
                media_type=media_type,
                max_size_bytes=output_bytes,
            )
            for name, media_type in sorted((declared_outputs or {}).items())
        ]
        if not output_declarations:
            output_declarations.append(
                OutputDeclaration(
                    name="artifact",
                    media_type=artifact_media_type,
                    max_size_bytes=output_bytes,
                )
            )
        output_declarations.append(
            OutputDeclaration(
                name="capability-result",
                media_type="application/json",
                max_size_bytes=512 * 1024,
            )
        )
        spec = ExecutionSpec(
            execution_attempt_id=attempt_id,
            workflow_run_id=f"workflow-{request_id}",
            step_run_id=f"step-{uuid.uuid4()}",
            tenant_id=self.tenant_id,
            project_id=self.project_id,
            expected_base_revision_id=expected_base_revision_id,
            idempotency_key=f"capability:{source.sha256}",
            capability=f"mcad.{capability}",
            operation=operation,
            mode=mode,
            source=source,
            inputs=tuple(declarations),
            outputs=tuple(output_declarations),
            runtime=RuntimeRequirement(
                image_digest=snapshot.image_digest,
                platform=snapshot.platform,
            ),
            limits=ResourceLimits.from_configured_memory(settings.sandbox_memory_limit,
                timeout_seconds=timeout_seconds,
                memory_bytes=(
                    1536 * 1024 * 1024
                    if operation == "snapshot"
                    else 1024 * 1024 * 1024
                ),
                cpu_millis=2000,
                pids=512 if operation == "snapshot" else 256,
                output_bytes=output_bytes,
            ),
            metadata={"request_id": request_id},
        )
        execution = await self.backend.execute(
            spec,
            materialized_inputs=materialized,
        )
        metadata: dict[str, Any] = {}
        metadata_path = execution.files.get("capability-result")
        if metadata_path and metadata_path.is_file():
            parsed = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                metadata = parsed
        return CapabilityExecutionOutcome(execution=execution, metadata=metadata)
