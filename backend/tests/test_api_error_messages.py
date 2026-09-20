import httpx
from openai import APITimeoutError, RateLimitError

from app.api.error_messages import (
    generation_error_http_status,
    public_generation_error,
)
import pytest


def test_model_timeout_has_actionable_public_message():
    error = APITimeoutError(request=httpx.Request("POST", "https://api.example.test/v1/chat/completions"))

    payload = public_generation_error(error)

    assert payload["type"] == "TimeoutError"
    assert "未设置模型 API 等待时限" in payload["message"]
    assert "LLM_TIMEOUT_S" not in payload["message"]


def test_unexpected_error_keeps_original_type_and_message():
    payload = public_generation_error(ValueError("bad input"))

    assert payload == {"type": "ValueError", "message": "bad input"}


def test_document_worker_unavailability_is_retryable_503_without_masking_internal_errors():
    from app.api.documents import public_error
    from app.temporal_client import TemporalWorkerUnavailable

    result = public_error(TemporalWorkerUnavailable("exact queue has no poller"))
    assert result.status_code == 503
    assert result.detail["code"] == "workflow_worker_unavailable" and result.detail["retryable"]
    with pytest.raises(RuntimeError, match="unrelated bug"):
        public_error(RuntimeError("unrelated bug"))


def test_provider_quota_error_is_unwrapped_and_redacted():
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(429, request=request)
    provider_error = RateLimitError(
        "account ak-secret suspended due to insufficient balance",
        response=response,
        body={
            "error": {
                "type": "exceeded_current_quota_error",
                "message": "insufficient balance for ak-secret",
            }
        },
    )
    wrapped = ValueError("planner model did not return a valid CAD plan")
    wrapped.__cause__ = provider_error

    payload = public_generation_error(wrapped)

    assert payload["type"] == "ProviderQuotaError"
    assert "额度不足" in payload["message"]
    assert "ak-secret" not in payload["message"]
    assert generation_error_http_status(payload) == 503
