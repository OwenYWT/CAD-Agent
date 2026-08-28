from contextvars import ContextVar
import hashlib
import json
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


_last_chat_completion_provenance: ContextVar[dict[str, Any] | None] = (
    ContextVar("last_chat_completion_provenance", default=None)
)


def reset_chat_completion_provenance() -> None:
    """Clear completion metadata in the current async task context."""
    _last_chat_completion_provenance.set(None)


def get_last_chat_completion_provenance() -> dict[str, Any] | None:
    """Return non-secret metadata for the latest completion in this task."""
    value = _last_chat_completion_provenance.get()
    return dict(value) if value is not None else None


def _completion_provenance(
    *,
    params: dict[str, Any],
    response: Any,
    provider: str,
) -> dict[str, Any]:
    request_payload = json.dumps(
        params,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    choices = list(getattr(response, "choices", ()) or ())
    first = choices[0] if choices else None
    message = getattr(first, "message", None)
    content = str(getattr(message, "content", "") or "")
    usage = getattr(response, "usage", None)
    if usage is None:
        usage_payload: dict[str, Any] = {}
    elif hasattr(usage, "model_dump"):
        usage_payload = usage.model_dump(mode="json", exclude_none=True)
    else:
        usage_payload = {
            key: value
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
            )
            if (value := getattr(usage, key, None)) is not None
        }
    return {
        "provider": provider,
        "model": str(getattr(response, "model", "") or params["model"]),
        "provider_response_id": (
            str(getattr(response, "id", "") or "") or None
        ),
        "request_hash": hashlib.sha256(request_payload).hexdigest(),
        "response_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "finish_reason": (
            str(getattr(first, "finish_reason", "") or "") or None
        ),
        "usage": usage_payload,
    }


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


def _is_kimi_model(model: str) -> bool:
    """Kimi identified by MODEL name, not by provider.

    Kimi is served by Moonshot directly and also resold through other
    OpenAI-compatible endpoints (DashScope hosts kimi-k3, kimi-k2.7-code and
    friends). Deciding on the provider alone missed those, and Kimi rejects
    `temperature` outright with a 400 -- which failed every request rather than
    degrading. The model name travels with the model, so key off that.
    """
    name = model.lower()
    return name.startswith("kimi") or name.startswith("moonshot")


def _is_moonshot_provider(llm_settings: Settings) -> bool:
    return llm_settings.normalized_llm_provider == "moonshot"


def _rejects_sampling_params(model: str, llm_settings: Settings) -> bool:
    """Models that refuse `temperature` and want `max_completion_tokens`."""
    return (
        _is_gpt5_model(model)
        or _is_kimi_model(model)
        or _is_moonshot_provider(llm_settings)
    )


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

    if _rejects_sampling_params(model, llm_settings):
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
                response = await self._raw_completions.create(**params)
                _last_chat_completion_provenance.set(
                    _completion_provenance(
                        params=params,
                        response=response,
                        provider=self._settings.normalized_llm_provider,
                    )
                )
                return response
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
            base_url=llm_settings.effective_llm_base_url,
            timeout=llm_settings.llm_timeout_s,
            max_retries=0,
        )

    return LLMClientAdapter(raw_client, llm_settings)
