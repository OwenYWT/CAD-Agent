"""Tests for LLM provider configuration."""

from app.services import llm_usage

from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, RateLimitError

from app.config import Settings
from app.llm import (
    ChatCompletionAdapter,
    build_chat_params,
    create_llm_client,
    get_last_chat_completion_provenance,
    reset_chat_completion_provenance,
)


def test_azure_credentials_are_detected():
    settings = Settings(
        llm_provider="azure",
        azure_openai_endpoint="https://example.openai.azure.com/",
        azure_openai_api_key="azure-key",
        azure_openai_api_version="2025-03-01-preview",
    )

    assert settings.has_llm_credentials is True


def test_moonshot_is_default_provider():
    settings = Settings(_env_file=None, moonshot_api_key="moonshot-key")

    assert settings.normalized_llm_provider == "moonshot"
    assert settings.llm_model == "kimi-k2.7-code"
    assert settings.vision_model == ""
    assert settings.effective_vision_model == settings.llm_model
    assert settings.llm_base_url == "https://api.moonshot.cn/v1"
    assert settings.llm_api_key == "moonshot-key"
    assert settings.has_llm_credentials is True


def test_vision_model_can_be_configured_without_second_credential():
    settings = Settings(
        _env_file=None,
        moonshot_api_key="moonshot-key",
        llm_model="kimi-k2.7-code",
        vision_model="moonshot-v1-32k-vision-preview",
    )

    assert settings.effective_vision_model == "moonshot-v1-32k-vision-preview"
    assert settings.llm_api_key == "moonshot-key"


def test_empty_vision_model_explicitly_reuses_primary_model():
    settings = Settings(
        _env_file=None,
        moonshot_api_key="moonshot-key",
        llm_model="multimodal-primary",
        vision_model="",
    )

    assert settings.effective_vision_model == "multimodal-primary"


def test_openai_compatible_still_uses_dashscope_key():
    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        dashscope_api_key="compat-key",
        llm_base_url="https://compat.example/v1",
    )

    assert settings.llm_api_key == "compat-key"
    assert settings.has_llm_credentials is True


def test_azure_client_type_is_selected():
    settings = Settings(
        llm_provider="azure",
        azure_openai_endpoint="https://example.openai.azure.com/",
        azure_openai_api_key="azure-key",
        azure_openai_api_version="2025-03-01-preview",
    )

    client = create_llm_client(settings, sink=llm_usage)

    assert client.raw_client.__class__.__name__ == "AsyncAzureOpenAI"


def test_moonshot_client_uses_async_openai():
    settings = Settings(_env_file=None, moonshot_api_key="moonshot-key")

    client = create_llm_client(settings, sink=llm_usage)

    assert client.raw_client.__class__.__name__ == "AsyncOpenAI"
    assert str(client.raw_client.base_url).startswith("https://api.moonshot.cn/v1")
    assert client.raw_client.max_retries == 0


def test_gpt5_omits_output_cap_and_preserves_reasoning_effort():
    params = build_chat_params(
        model="gpt-5",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=1234,
        temperature=0.2,
        llm_settings=Settings(llm_reasoning_effort="minimal"),
    )

    assert params["model"] == "gpt-5"
    assert "max_completion_tokens" not in params
    assert params["reasoning_effort"] == "minimal"
    assert "max_tokens" not in params
    assert "temperature" not in params


def test_non_gpt5_keeps_legacy_chat_params():
    params = build_chat_params(
        model="qwen-plus",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=1234,
        temperature=0.2,
        llm_settings=Settings(_env_file=None, llm_provider="openai_compatible"),
    )

    assert "max_tokens" not in params
    assert params["temperature"] == 0.2
    assert "max_completion_tokens" not in params
    assert "reasoning_effort" not in params


def test_moonshot_drops_temperature_param():
    params = build_chat_params(
        model="kimi-k2.7-code",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=1234,
        temperature=0.2,
        llm_settings=Settings(_env_file=None, llm_provider="moonshot"),
    )

    assert params["model"] == "kimi-k2.7-code"
    assert "max_completion_tokens" not in params
    assert "max_tokens" not in params
    assert "temperature" not in params


@pytest.mark.asyncio
async def test_chat_adapter_never_retries_quota_failure():
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(429, request=request)

    class QuotaCompletions:
        calls = 0

        async def create(self, **_kwargs):
            self.calls += 1
            raise RateLimitError(
                "insufficient balance",
                response=response,
                body={"error": {"type": "exceeded_current_quota_error"}},
            )

    raw = QuotaCompletions()
    adapter = ChatCompletionAdapter(
        raw,
        Settings(_env_file=None, llm_max_retries=2),
    sink=llm_usage)

    with pytest.raises(RateLimitError):
        await adapter.create(
            model="qwen-plus",
            messages=[{"role": "user", "content": "hello"}],
        )
    assert raw.calls == 1


@pytest.mark.asyncio
async def test_chat_adapter_retries_transient_connection_failure_with_bound():
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")

    class TransientCompletions:
        calls = 0

        async def create(self, **_kwargs):
            self.calls += 1
            if self.calls < 3:
                raise APIConnectionError(request=request)
            return SimpleNamespace(choices=[])

    raw = TransientCompletions()
    adapter = ChatCompletionAdapter(
        raw,
        Settings(_env_file=None, llm_max_retries=2),
    sink=llm_usage)

    result = await adapter.create(
        model="qwen-plus",
        messages=[{"role": "user", "content": "hello"}],
    )

    assert result.choices == []
    assert raw.calls == 3


@pytest.mark.asyncio
async def test_chat_adapter_records_non_secret_completion_provenance():
    class RecordedCompletions:
        async def create(self, **_kwargs):
            return SimpleNamespace(
                id="chatcmpl-controlled",
                model="controlled-model-2026-08-12",
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content="result = 42"),
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=7,
                    completion_tokens=3,
                    total_tokens=10,
                ),
            )

    reset_chat_completion_provenance()
    adapter = ChatCompletionAdapter(
        RecordedCompletions(),
        Settings(_env_file=None, llm_provider="openai_compatible"),
    sink=llm_usage)
    await adapter.create(
        model="requested-model",
        messages=[{"role": "user", "content": "create"}],
    )

    provenance = get_last_chat_completion_provenance()
    assert provenance is not None
    assert provenance["provider"] == "openai_compatible"
    assert provenance["model"] == "controlled-model-2026-08-12"
    assert provenance["provider_response_id"] == "chatcmpl-controlled"
    assert len(provenance["request_hash"]) == 64
    assert len(provenance["response_hash"]) == 64
    assert provenance["usage"]["total_tokens"] == 10
