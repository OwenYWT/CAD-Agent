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


def test_moonshot_is_default_provider():
    settings = Settings(_env_file=None, moonshot_api_key="moonshot-key")

    assert settings.normalized_llm_provider == "moonshot"
    assert settings.llm_model == "kimi-k2.7-code"
    assert settings.llm_base_url == "https://api.moonshot.cn/v1"
    assert settings.llm_api_key == "moonshot-key"
    assert settings.has_llm_credentials is True


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

    client = create_llm_client(settings)

    assert client.raw_client.__class__.__name__ == "AsyncAzureOpenAI"


def test_moonshot_client_uses_async_openai():
    settings = Settings(_env_file=None, moonshot_api_key="moonshot-key")

    client = create_llm_client(settings)

    assert client.raw_client.__class__.__name__ == "AsyncOpenAI"
    assert str(client.raw_client.base_url).startswith("https://api.moonshot.cn/v1")


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
        llm_settings=Settings(_env_file=None, llm_provider="openai_compatible"),
    )

    assert params["max_tokens"] == 1234
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
    assert params["max_completion_tokens"] == 1234
    assert "max_tokens" not in params
    assert "temperature" not in params
