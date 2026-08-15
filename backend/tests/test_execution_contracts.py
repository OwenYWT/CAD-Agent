import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.execution.canonical import canonical_json_bytes, canonical_sha256
from app.execution.contracts import (
    ArtifactInput,
    ArtifactOutput,
    ExecutionError,
    ExecutionErrorCategory,
    ExecutionLeaseEnvelope,
    ExecutionResult,
    ExecutionSource,
    ExecutionSpec,
    ExecutionStatus,
    OutputDeclaration,
    ResourceLimits,
    RuntimeProvenance,
    RuntimeRequirement,
    SignedTransfer,
)


def _source(code: str = "result = cq.Workplane('XY').box(1, 1, 1)") -> ExecutionSource:
    return ExecutionSource(
        language="python",
        code=code,
        sha256=hashlib.sha256(code.encode("utf-8")).hexdigest(),
    )


def _spec(**updates) -> ExecutionSpec:
    values = {
        "execution_attempt_id": "attempt-01",
        "workflow_run_id": "workflow-01",
        "step_run_id": "step-01",
        "tenant_id": "tenant-01",
        "project_id": "project-01",
        "idempotency_key": "workflow-01:model:1",
        "capability": "mcad.cadquery",
        "operation": "generate",
        "mode": "3d",
        "source": _source(),
        "inputs": (
            ArtifactInput(
                artifact_id="input-01",
                filename="reference.step",
                sha256="a" * 64,
                size_bytes=128,
                media_type="model/step",
            ),
        ),
        "outputs": (
            OutputDeclaration(name="step", media_type="model/step", required=True),
            OutputDeclaration(name="stl", media_type="model/stl", required=True),
        ),
        "runtime": RuntimeRequirement(
            image_digest=f"sha256:{'b' * 64}",
            platform="linux/arm64",
        ),
        "limits": ResourceLimits(),
    }
    values.update(updates)
    return ExecutionSpec(**values)


def test_execution_spec_is_immutable_and_rejects_transport_secrets():
    spec = _spec()

    with pytest.raises(ValidationError):
        spec.operation = "modify"

    with pytest.raises(ValidationError):
        ExecutionSpec(**{**spec.model_dump(), "lease_token": "secret"})


def test_execution_source_hash_must_match_code():
    with pytest.raises(ValidationError, match="sha256"):
        ExecutionSource(language="python", code="result = 1", sha256="0" * 64)


def test_spec_hash_materializes_defaults_and_explicit_nulls():
    implicit = _spec()
    explicit = _spec(
        expected_base_revision_id=None,
        metadata={},
    )

    assert canonical_sha256(implicit) == canonical_sha256(explicit)
    assert b'"expected_base_revision_id":null' in canonical_json_bytes(implicit)


def test_lease_envelope_is_separate_from_persisted_spec():
    now = datetime.now(timezone.utc)
    lease = ExecutionLeaseEnvelope(
        execution_attempt_id="attempt-01",
        lease_token="lease-secret-value",
        lease_generation=2,
        expires_at=now + timedelta(minutes=5),
        input_downloads=(
            SignedTransfer(
                artifact_id="input-01",
                url="https://objects.invalid/signed-input",
                expires_at=now + timedelta(minutes=5),
                method="GET",
            ),
        ),
        output_uploads=(
            SignedTransfer(
                artifact_id="output-step",
                url="https://objects.invalid/signed-output",
                expires_at=now + timedelta(minutes=5),
                method="PUT",
            ),
        ),
        callback_credential="callback-secret-value",
    )

    spec_bytes = canonical_json_bytes(_spec())

    assert lease.lease_token == "lease-secret-value"
    assert b"lease-secret-value" not in spec_bytes
    assert b"callback-secret-value" not in spec_bytes
    assert b"signed-input" not in spec_bytes


def test_execution_result_records_terminal_artifacts_error_and_provenance():
    result = ExecutionResult(
        execution_attempt_id="attempt-01",
        status=ExecutionStatus.FAILED,
        outputs=(
            ArtifactOutput(
                upload_id="upload-log",
                name="execution.log",
                sha256="c" * 64,
                size_bytes=42,
                media_type="text/plain",
            ),
        ),
        error=ExecutionError(
            category=ExecutionErrorCategory.INFRASTRUCTURE,
            code="runtime_unavailable",
            message="Podman socket unavailable",
            evidence={"exit_code": "125"},
        ),
        provenance=RuntimeProvenance(
            image_digest=f"sha256:{'b' * 64}",
            platform="linux/arm64",
            versions={"python": "3.11.15", "cadquery": "2.8.0"},
            input_hash="d" * 64,
            code_hash=_source().sha256,
        ),
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )

    assert result.status is ExecutionStatus.FAILED
    assert result.error.category is ExecutionErrorCategory.INFRASTRUCTURE
    assert result.outputs[0].sha256 == "c" * 64


def test_success_result_cannot_contain_error():
    with pytest.raises(ValidationError):
        ExecutionResult(
            execution_attempt_id="attempt-01",
            status=ExecutionStatus.SUCCEEDED,
            error=ExecutionError(
                category=ExecutionErrorCategory.USER_CODE,
                code="unexpected",
                message="must not coexist with success",
            ),
        )


def test_canonical_vectors_match_cross_language_fixtures():
    fixture = Path(__file__).parent / "fixtures" / "execution_canonical_vectors.json"
    vectors = json.loads(fixture.read_text(encoding="utf-8"))

    for vector in vectors:
        encoded = canonical_json_bytes(vector["value"])
        assert encoded.decode("utf-8") == vector["canonical"], vector["name"]
        assert canonical_sha256(vector["value"]) == vector["sha256"], vector["name"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_numbers(value):
    with pytest.raises(ValueError, match="finite"):
        canonical_json_bytes({"value": value})


def test_canonical_json_requires_decimal_values_as_strings():
    with pytest.raises(TypeError, match="Decimal"):
        canonical_json_bytes({"value": Decimal("1.25")})


def test_canonical_json_rejects_unicode_key_collision_after_nfc():
    with pytest.raises(ValueError, match="collision"):
        canonical_json_bytes({"é": 1, "é": 2})
