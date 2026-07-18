"""Stable, non-leaky Fusion connector error mapping."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .contract import CadError


@dataclass(frozen=True)
class ErrorSpec:
    category: str
    retryable: bool
    http_status: int


ERROR_SPECS: dict[str, ErrorSpec] = {
    "AUTH_REQUIRED": ErrorSpec("auth", False, 401),
    "CONNECTOR_OFFLINE": ErrorSpec("offline", True, 503),
    "NO_ACTIVE_DOCUMENT": ErrorSpec("not_found", False, 409),
    "NO_ACTIVE_DESIGN": ErrorSpec("not_found", False, 409),
    "DOCUMENT_MISMATCH": ErrorSpec("conflict", False, 409),
    "DOCUMENT_UNSAVED": ErrorSpec("validation", False, 409),
    "DOCUMENT_READ_ONLY": ErrorSpec("read_only", False, 409),
    "CONFIGURATION_READ_ONLY": ErrorSpec("read_only", False, 409),
    "TARGET_NOT_FOUND": ErrorSpec("not_found", False, 404),
    "TARGET_AMBIGUOUS": ErrorSpec("conflict", False, 409),
    "PARAMETER_NOT_FOUND": ErrorSpec("not_found", False, 404),
    "FEATURE_NOT_FOUND": ErrorSpec("not_found", False, 404),
    "INVALID_ACTION": ErrorSpec("validation", False, 422),
    "UNSUPPORTED_ACTION": ErrorSpec("validation", False, 422),
    "UNSUPPORTED_FUSION_VERSION": ErrorSpec("validation", False, 426),
    "APPROVAL_REQUIRED": ErrorSpec("approval", False, 409),
    "APPROVAL_INVALID": ErrorSpec("approval", False, 409),
    "APPROVAL_EXPIRED": ErrorSpec("approval", False, 409),
    "APPROVAL_ALREADY_USED": ErrorSpec("approval", False, 409),
    "IDEMPOTENCY_CONFLICT": ErrorSpec("conflict", False, 409),
    "REQUEST_CANCELLED": ErrorSpec("cancel", False, 409),
    "REQUEST_TIMEOUT": ErrorSpec("timeout", True, 504),
    "FUSION_API_ERROR": ErrorSpec("fusion", False, 500),
    "COMPUTE_FAILED": ErrorSpec("fusion", False, 422),
    "VERIFICATION_FAILED": ErrorSpec("verification", False, 422),
    "EXPORT_FAILED": ErrorSpec("artifact", False, 422),
    "ARTIFACT_INVALID": ErrorSpec("artifact", False, 422),
    "PATH_NOT_ALLOWED": ErrorSpec("artifact", False, 422),
    "ARTIFACT_ROOT_MISMATCH": ErrorSpec("artifact", False, 409),
    "ARTIFACT_QUOTA_EXCEEDED": ErrorSpec("artifact", True, 507),
    "CLOUD_NOT_CONFIGURED": ErrorSpec("cloud", False, 503),
    "CLOUD_AUTH_REQUIRED": ErrorSpec("cloud", False, 401),
    "CLOUD_RATE_LIMITED": ErrorSpec("cloud", True, 429),
    "PROTOCOL_MISMATCH": ErrorSpec("validation", False, 426),
    "STALE_LEASE": ErrorSpec("conflict", False, 409),
    "CONNECTOR_SELECTION_REQUIRED": ErrorSpec("conflict", True, 409),
    "BASELINE_NOT_FOUND": ErrorSpec("not_found", False, 404),
    "SOURCE_RESULT_NOT_FOUND": ErrorSpec("not_found", False, 404),
    "CREDENTIAL_ROLE_MISMATCH": ErrorSpec("auth", False, 403),
    "OWNER_MISMATCH": ErrorSpec("not_found", False, 404),
    "QUEUE_FULL": ErrorSpec("rate_limit", True, 429),
    "RATE_LIMITED": ErrorSpec("rate_limit", True, 429),
}


class FusionConnectorError(Exception):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        if code not in ERROR_SPECS:
            raise ValueError(f"unknown stable error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @property
    def http_status(self) -> int:
        return ERROR_SPECS[self.code].http_status

    def to_model(self) -> CadError:
        spec = ERROR_SPECS[self.code]
        return CadError(
            code=self.code,
            message=self.message,
            category=spec.category,
            retryable=spec.retryable,
            details=_safe_details(self.details),
        )


def _safe_details(details: dict[str, Any]) -> dict[str, Any]:
    forbidden = (
        "traceback", "exception", "token", "secret", "password", "authorization", "path", "input",
    )

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): clean(item)
                for key, item in value.items()
                if not any(marker in str(key).lower() for marker in forbidden)
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, (type(None), bool, int, float, str)):
            return value
        return str(type(value).__name__)

    return clean(details)


def map_exception(exc: Exception) -> FusionConnectorError:
    if isinstance(exc, FusionConnectorError):
        return exc
    if isinstance(exc, ValidationError):
        return FusionConnectorError(
            "INVALID_ACTION",
            "Fusion connector request validation failed",
            details={"errors": exc.errors(include_url=False, include_context=False, include_input=False)},
        )
    if isinstance(exc, TimeoutError):
        return FusionConnectorError("REQUEST_TIMEOUT", "Fusion connector request timed out")
    return FusionConnectorError("FUSION_API_ERROR", "Fusion operation failed")
