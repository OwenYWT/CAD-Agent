from typing import Any

from openai import AsyncAzureOpenAI, AsyncOpenAI

from app.config import Settings, settings


def _is_gpt5_model(model: str) -> bool:
    return model.lower().startswith("gpt-5")


def build_chat_params(
    *,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int | None = None,
    temperature: float | None = None,
    llm_settings: Settings = settings,
    **extra: Any,
) -> dict[str, Any]:
    params: dict[str, Any] = {"model": model, "messages": messages}
    params.update(extra)

    if _is_gpt5_model(model):
        if max_tokens is not None and "max_completion_tokens" not in params:
            params["max_completion_tokens"] = max_tokens
        if llm_settings.llm_reasoning_effort and "reasoning_effort" not in params:
            params["reasoning_effort"] = llm_settings.llm_reasoning_effort
        params.pop("max_tokens", None)
        params.pop("temperature", None)
        return params

    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    if temperature is not None:
        params["temperature"] = temperature
    return params


class ChatCompletionAdapter:
    def __init__(self, raw_completions: Any, llm_settings: Settings):
        self._raw_completions = raw_completions
        self._settings = llm_settings

    async def create(self, **kwargs: Any):
        max_tokens = kwargs.pop("max_tokens", None)
        temperature = kwargs.pop("temperature", None)
        params = build_chat_params(
            model=kwargs.pop("model"),
            messages=kwargs.pop("messages"),
            max_tokens=max_tokens,
            temperature=temperature,
            llm_settings=self._settings,
            **kwargs,
        )
        return await self._raw_completions.create(**params)


class ChatAdapter:
    def __init__(self, raw_chat: Any, llm_settings: Settings):
        self.completions = ChatCompletionAdapter(raw_chat.completions, llm_settings)


class LLMClientAdapter:
    def __init__(self, raw_client: Any, llm_settings: Settings):
        self.raw_client = raw_client
        self.chat = ChatAdapter(raw_client.chat, llm_settings)


def create_llm_client(llm_settings: Settings = settings):
    if not llm_settings.has_llm_credentials:
        raise RuntimeError(llm_settings.llm_credentials_error)

    if llm_settings.normalized_llm_provider == "azure":
        raw_client = AsyncAzureOpenAI(
            azure_endpoint=llm_settings.azure_openai_endpoint,
            api_key=llm_settings.azure_openai_api_key,
            api_version=llm_settings.azure_openai_api_version,
            timeout=llm_settings.llm_timeout_s,
            max_retries=llm_settings.llm_max_retries,
        )
    else:
        raw_client = AsyncOpenAI(
            api_key=llm_settings.dashscope_api_key,
            base_url=llm_settings.llm_base_url,
            timeout=llm_settings.llm_timeout_s,
            max_retries=llm_settings.llm_max_retries,
        )

    return LLMClientAdapter(raw_client, llm_settings)
