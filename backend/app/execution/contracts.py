"""Stable wire contracts shared by local, cloud, and private execution backends."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ImageDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
JsonPrimitive = str | int | float | bool | None
ErrorDetailValue = JsonPrimitive | list[str] | list[int]


class FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExecutionStatus(str, Enum):
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    UPLOADING = "UPLOADING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    OOM = "OOM"
    CANCELLED = "CANCELLED"
    LEASE_LOST = "LEASE_LOST"
    ARTIFACT_REJECTED = "ARTIFACT_REJECTED"


class ExecutionErrorCategory(str, Enum):
    USER_INPUT = "user_input"
    USER_CODE = "user_code"
    CAD_KERNEL = "cad_kernel"
    VALIDATION = "validation"
    INFRASTRUCTURE = "infrastructure"
    TIMEOUT = "timeout"
    RESOURCE = "resource"
    ARTIFACT = "artifact"
    CANCELLATION = "cancellation"
    INTERNAL = "internal"


class ExecutionSource(FrozenContract):
    language: Literal["python", "javascript", "json"]
    code: str = Field(min_length=1, max_length=2_000_000)
    sha256: Sha256

    @model_validator(mode="after")
    def verify_hash(self) -> "ExecutionSource":
        actual = hashlib.sha256(self.code.encode("utf-8")).hexdigest()
        if actual != self.sha256:
            raise ValueError("source sha256 does not match UTF-8 code bytes")
        return self


class ArtifactInput(FrozenContract):
    artifact_id: Identifier
    filename: str = Field(min_length=1, max_length=255)
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=200)


class OutputDeclaration(FrozenContract):
    name: Identifier
    media_type: str = Field(min_length=1, max_length=200)
    required: bool = True
    max_size_bytes: int | None = Field(default=None, ge=1)


class RuntimeRequirement(FrozenContract):
    image_digest: ImageDigest
    platform: Literal["linux/amd64", "linux/arm64"]
    sandbox_tier: str | None = Field(default=None, max_length=100)


class ResourceLimits(FrozenContract):
    timeout_seconds: int | None = Field(default=60, ge=1, le=3600)
    memory_bytes: int = Field(default=512 * 1024 * 1024, ge=64 * 1024 * 1024)
    cpu_millis: int = Field(default=1000, ge=100, le=64_000)
    pids: int = Field(default=128, ge=16, le=4096)
    output_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)

    @classmethod
    def from_configured_memory(cls, memory_limit: str, **limits) -> "ResourceLimits":
        """Resolve deployment memory; preserve existing operation-specific floors.

        Wire defaults stay unchanged for retained specifications and replay.
        Composition must explicitly pass the deployed configuration for new work.
        """
        from decimal import Decimal
        import re
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([bkmg]?)", memory_limit.strip().lower())
        if match is None:
            raise ValueError("sandbox memory must be bytes or a b/k/m/g quantity")
        multiplier = {"":1,"b":1,"k":1024,"m":1024**2,"g":1024**3}[match[2]]
        amount = Decimal(match[1]) * multiplier
        if amount != amount.to_integral_value() or amount < 64*1024**2:
            raise ValueError("sandbox memory must be whole bytes and at least 64 MiB")
        limits["memory_bytes"] = max(int(amount), limits.get("memory_bytes", 0))
        return cls(**limits)


class ExecutionSpec(FrozenContract):
    schema_version: Literal["execution-spec.v1"] = "execution-spec.v1"
    execution_attempt_id: Identifier
    workflow_run_id: Identifier
    step_run_id: Identifier
    tenant_id: Identifier
    project_id: Identifier
    expected_base_revision_id: Identifier | None = None
    idempotency_key: Identifier
    capability: Identifier
    operation: Identifier
    mode: Literal["2d", "3d", "analysis"]
    source: ExecutionSource
    inputs: tuple[ArtifactInput, ...] = ()
    outputs: tuple[OutputDeclaration, ...] = ()
    runtime: RuntimeRequirement
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    metadata: dict[str, JsonPrimitive] = Field(default_factory=dict)


class SignedTransfer(FrozenContract):
    artifact_id: Identifier
    url: str = Field(min_length=1, max_length=4096)
    expires_at: datetime
    method: Literal["GET", "PUT"]
    headers: dict[str, str] = Field(default_factory=dict)


class ExecutionLeaseEnvelope(FrozenContract):
    schema_version: Literal["execution-lease.v1"] = "execution-lease.v1"
    execution_attempt_id: Identifier
    lease_token: str = Field(min_length=16, max_length=1024)
    lease_generation: int = Field(ge=1)
    expires_at: datetime
    input_downloads: tuple[SignedTransfer, ...] = ()
    output_uploads: tuple[SignedTransfer, ...] = ()
    callback_credential: str = Field(min_length=16, max_length=4096)


class ArtifactOutput(FrozenContract):
    upload_id: Identifier
    name: str = Field(min_length=1, max_length=255)
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=200)


class ExecutionError(FrozenContract):
    category: ExecutionErrorCategory
    code: Identifier
    message: str = Field(min_length=1, max_length=4000)
    operation_id: str | None = Field(default=None, max_length=200)
    action: str | None = Field(default=None, max_length=200)
    details: dict[str, ErrorDetailValue] = Field(default_factory=dict)
    retryable: bool = False
    evidence: dict[str, JsonPrimitive] = Field(default_factory=dict)


class RuntimeProvenance(FrozenContract):
    image_digest: ImageDigest
    platform: Literal["linux/amd64", "linux/arm64"]
    versions: dict[str, str] = Field(default_factory=dict)
    input_hash: Sha256
    code_hash: Sha256
    sandbox_tier: str | None = Field(default=None, max_length=100)


class ExecutionResult(FrozenContract):
    schema_version: Literal["execution-result.v1"] = "execution-result.v1"
    execution_attempt_id: Identifier
    status: ExecutionStatus
    outputs: tuple[ArtifactOutput, ...] = ()
    validations: tuple[dict[str, JsonPrimitive], ...] = ()
    metrics: dict[str, JsonPrimitive] = Field(default_factory=dict)
    error: ExecutionError | None = None
    provenance: RuntimeProvenance | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def validate_terminal_result(self) -> "ExecutionResult":
        terminal = {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMED_OUT,
            ExecutionStatus.OOM,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.LEASE_LOST,
            ExecutionStatus.ARTIFACT_REJECTED,
        }
        if self.status not in terminal:
            raise ValueError("ExecutionResult status must be terminal")
        if self.status is ExecutionStatus.SUCCEEDED and self.error is not None:
            raise ValueError("successful ExecutionResult cannot contain an error")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at cannot be earlier than started_at")
        return self
