"""Tests for LLM provider configuration."""

from app.config import Settings
from app.llm import build_chat_params, create_llm_client


def test_azure_credentials_are_detected():
    settings = Settings(
        llm_provider="azure",
        azure_openai_endpoint="https://example.openai.azure.com/",
        azure_openai_api_key="azure-key",
        azure_openai_api_version="2025-03-01-preview",
    )

    assert settings.has_llm_credentials is True


def test_azure_client_type_is_selected():
    settings = Settings(
        llm_provider="azure",
        azure_openai_endpoint="https://example.openai.azure.com/",
        azure_openai_api_key="azure-key",
        azure_openai_api_version="2025-03-01-preview",
    )

    client = create_llm_client(settings)

    assert client.raw_client.__class__.__name__ == "AsyncAzureOpenAI"


def test_gpt5_uses_max_completion_tokens_and_reasoning_effort():
    params = build_chat_params(
        model="gpt-5",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=1234,
        temperature=0.2,
        llm_settings=Settings(llm_reasoning_effort="minimal"),
    )

    assert params["model"] == "gpt-5"
    assert params["max_completion_tokens"] == 1234
    assert params["reasoning_effort"] == "minimal"
    assert "max_tokens" not in params
    assert "temperature" not in params


def test_non_gpt5_keeps_legacy_chat_params():
    params = build_chat_params(
        model="qwen-plus",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=1234,
        temperature=0.2,
        llm_settings=Settings(),
    )

    assert params["max_tokens"] == 1234
    assert params["temperature"] == 0.2
    assert "max_completion_tokens" not in params
    assert "reasoning_effort" not in params
