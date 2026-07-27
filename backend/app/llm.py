from typing import Any

from openai import AsyncAzureOpenAI, AsyncOpenAI, OpenAIError, RateLimitError

from app.config import Settings, settings


_QUOTA_MARKERS = (
    "insufficient balance",
    "insufficient_balance",
    "exceeded_current_quota",
    "billing",
    "quota exceeded",
)


def find_provider_exception(exc: BaseException) -> OpenAIError | None:
    """Return the first OpenAI-compatible provider exception in a wrapped chain."""

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OpenAIError):
            return current
        current = current.__cause__ or current.__context__
    return None


def is_provider_quota_error(exc: BaseException) -> bool:
    provider_error = find_provider_exception(exc)
    if not isinstance(provider_error, RateLimitError):
        return False
    body = getattr(provider_error, "body", None)
    text = f"{provider_error} {body}".lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


def is_nonretryable_provider_error(exc: BaseException) -> bool:
    """Identify provider failures that another identical request cannot repair."""

    provider_error = find_provider_exception(exc)
    if provider_error is None:
        return False
    if is_provider_quota_error(provider_error):
        return True
    status_code = getattr(provider_error, "status_code", None)
    return status_code in {400, 401, 403, 404, 422}


def _is_gpt5_model(model: str) -> bool:
    return model.lower().startswith("gpt-5")


def _is_moonshot_provider(llm_settings: Settings) -> bool:
    return llm_settings.normalized_llm_provider == "moonshot"


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

    if _is_gpt5_model(model) or _is_moonshot_provider(llm_settings):
        if max_tokens is not None and "max_completion_tokens" not in params:
            params["max_completion_tokens"] = max_tokens
        if _is_gpt5_model(model) and llm_settings.llm_reasoning_effort and "reasoning_effort" not in params:
            params["reasoning_effort"] = llm_settings.llm_reasoning_effort
        params.pop("max_tokens", None)
        params.pop("temperature", None)
        return params

    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    if temperature is not None and not _is_moonshot_provider(llm_settings):
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
        for attempt in range(self._settings.llm_max_retries + 1):
            try:
                return await self._raw_completions.create(**params)
            except OpenAIError as exc:
                if (
                    is_nonretryable_provider_error(exc)
                    or attempt >= self._settings.llm_max_retries
                ):
                    raise


class ChatAdapter:
    def __init__(self, raw_chat: Any, llm_settings: Settings):
        self.completions = ChatCompletionAdapter(raw_chat.completions, llm_settings)


class LLMClientAdapter:
    def __init__(self, raw_client: Any, llm_settings: Settings):
        self.raw_client = raw_client
        self._settings = llm_settings
        self.chat = ChatAdapter(raw_client.chat, llm_settings)

    @property
    def max_retries(self) -> int:
        return self._settings.llm_max_retries

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw_client, name)


def create_llm_client(llm_settings: Settings = settings):
    if not llm_settings.has_llm_credentials:
        raise RuntimeError(llm_settings.llm_credentials_error)

    if llm_settings.normalized_llm_provider == "azure":
        raw_client = AsyncAzureOpenAI(
            azure_endpoint=llm_settings.azure_openai_endpoint,
            api_key=llm_settings.azure_openai_api_key,
            api_version=llm_settings.azure_openai_api_version,
            timeout=llm_settings.llm_timeout_s,
            # The adapter owns retries so quota/auth/config failures can be
            # rejected immediately instead of being retried blindly by the SDK.
            max_retries=0,
        )
    else:
        raw_client = AsyncOpenAI(
            api_key=llm_settings.llm_api_key,
            base_url=llm_settings.llm_base_url,
            timeout=llm_settings.llm_timeout_s,
            max_retries=0,
        )

    return LLMClientAdapter(raw_client, llm_settings)
