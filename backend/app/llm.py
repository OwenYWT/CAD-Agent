from contextvars import ContextVar
import hashlib
import json
import time
from typing import Any

from openai import AsyncAzureOpenAI, AsyncOpenAI, OpenAIError, RateLimitError
from openai.types.chat import ChatCompletion

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
    tool_calls = getattr(message, "tool_calls", None)
    response_bytes = content.encode("utf-8")
    if tool_calls:
        response_bytes = json.dumps({"content": content, "tool_calls": [
            call.model_dump(mode="json", exclude_none=True) for call in tool_calls
        ]}, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
        "response_hash": hashlib.sha256(response_bytes).hexdigest(),
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
    # Output length is provider-owned. Ignore legacy callers and all aliases.
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        params.pop(key, None)
    if isinstance(params.get("extra_body"), dict):
        params["extra_body"] = {key: value for key, value in params["extra_body"].items()
                                if key not in {"max_tokens", "max_completion_tokens", "max_output_tokens"}}

    if _is_gpt5_model(model) or _is_moonshot_provider(llm_settings):
        if _is_gpt5_model(model) and llm_settings.llm_reasoning_effort and "reasoning_effort" not in params:
            params["reasoning_effort"] = llm_settings.llm_reasoning_effort
        params.pop("max_tokens", None)
        params.pop("temperature", None)
        return params

    if temperature is not None and not _is_moonshot_provider(llm_settings):
        params["temperature"] = temperature
    return params


from app.contracts.usage import UsageSink


class ChatCompletionAdapter:
    def __init__(self, raw_completions: Any, llm_settings: Settings, *, sink: UsageSink):
        self._usage = sink
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
        # Explicit None disables SDK/HTTP timeouts, including caller overrides.
        # Omitting this option would inherit the SDK or client default instead.
        params["timeout"] = None
        if params.get("stream"):
            params["stream_options"] = {**params.get("stream_options", {}), "include_usage": True}
        request_hash = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()
        reset_chat_completion_provenance()
        for attempt in range(self._settings.llm_max_retries + 1):
            handle = await self._usage.start_call(provider=self._settings.normalized_llm_provider,
                                      model=params["model"], request_hash=request_hash, attempt=attempt + 1)
            started = time.perf_counter()
            provenance = None
            error = None
            try:
                response = await self._raw_completions.create(**params)
                if params.get("stream"):
                    response = await _complete_stream(response)
                _last_chat_completion_provenance.set(
                    _completion_provenance(
                        params=params,
                        response=response,
                        provider=self._settings.normalized_llm_provider,
                    )
                )
                provenance = get_last_chat_completion_provenance()
                return response
            except OpenAIError as exc:
                error = exc
                if (
                    is_nonretryable_provider_error(exc)
                    or attempt >= self._settings.llm_max_retries
                ):
                    raise
            except BaseException as exc:
                error = exc
                raise
            finally:
                await self._usage.finish_call(handle, duration_ms=round((time.perf_counter() - started) * 1000),
                                  provenance=provenance, error=error)


async def _complete_stream(stream) -> ChatCompletion:
    """Accumulate actual provider chunks; a disconnected stream is never success.

    No API waiting deadline is imposed. Temporal still bounds the entire
    activity; cancellation closes the stream in the finally block below.
    """
    identity = None
    content, refusal, reasoning = [], [], []
    calls: dict[int, dict[str, Any]] = {}
    finish = None
    usage = None
    try:
        async for chunk in stream:
            if (
                not chunk.id
                and not chunk.model
                and not chunk.created
                and not chunk.choices
            ):
                if chunk.usage is not None:
                    usage = chunk.usage
                continue
            current = (chunk.id, chunk.model, chunk.created)
            if identity is None:
                identity = current
            if not chunk.id or current != identity:
                raise ValueError("model stream mixed completion identities")
            if chunk.usage is not None:
                usage = chunk.usage
            for choice in chunk.choices:
                if choice.index != 0 or choice.delta.function_call:
                    raise ValueError("model stream contains unsupported choices or legacy function calls")
                if finish is not None:
                    raise ValueError("model stream continued after its terminal choice")
                delta = choice.delta.content or ""
                content.append(delta)
                if choice.delta.refusal:
                    refusal.append(choice.delta.refusal)
                # Provider-owned context is round-tripped only to the model.
                # Kimi thinking tool conversations require this field on the
                # next assistant message; it is not user-facing task progress.
                reasoning_delta = getattr(choice.delta, "reasoning_content", None)
                if reasoning_delta is not None:
                    if not isinstance(reasoning_delta, str):
                        raise ValueError("model stream contains invalid reasoning context")
                    reasoning.append(reasoning_delta)
                for part in choice.delta.tool_calls or ():
                    if part.index < 0:
                        raise ValueError("model stream contains a negative tool index")
                    call = calls.setdefault(part.index, {"id": None, "type": None,
                        "function": {"name": "", "arguments": ""}})
                    for key in ("id", "type"):
                        value = getattr(part, key)
                        if value is not None:
                            if call[key] is not None and call[key] != value:
                                raise ValueError("model stream mixed tool identities")
                            call[key] = value
                    if part.function:
                        for key in ("name", "arguments"):
                            call["function"][key] += getattr(part.function, key) or ""
                finish = choice.finish_reason
    finally:
        await stream.close()
    if identity is None or finish is None:
        raise ValueError("model stream ended without a terminal provider response")
    ordered_calls = [calls[index] for index in sorted(calls)]
    if finish == "tool_calls" or calls:
        if finish not in {"tool_calls", "length", "content_filter"} or not calls:
            raise ValueError("model stream tool calls disagree with terminal reason")
        if finish == "tool_calls" and (
            sorted(calls) != list(range(len(calls)))
            or len({call["id"] for call in ordered_calls}) != len(calls)
            or any(not call["id"] or call["type"] != "function"
                   or not call["function"]["name"] for call in ordered_calls)
        ):
            raise ValueError("model stream ended with incomplete tool identities")
    # Truncated calls are not executable; preserve the reason for caller diagnostics.
    completed_calls = ordered_calls if finish == "tool_calls" else None
    return ChatCompletion(id=identity[0], model=identity[1], created=identity[2], object="chat.completion",
        choices=[{"index": 0, "finish_reason": finish,
                  "message": {"role": "assistant", "content": "".join(content),
                              "tool_calls": completed_calls, "refusal": "".join(refusal) or None,
                              **({"reasoning_content": "".join(reasoning)} if reasoning else {})}}],
        usage=usage)


class ChatAdapter:
    def __init__(self, raw_chat: Any, llm_settings: Settings, *, sink: UsageSink):
        self.completions = ChatCompletionAdapter(raw_chat.completions, llm_settings, sink=sink)


class LLMClientAdapter:
    def __init__(self, raw_client: Any, llm_settings: Settings, *, sink: UsageSink):
        self.raw_client = raw_client
        self._settings = llm_settings
        self.chat = ChatAdapter(raw_client.chat, llm_settings, sink=sink)

    @property
    def max_retries(self) -> int:
        return self._settings.llm_max_retries

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw_client, name)


def create_llm_client(llm_settings: Settings = settings, *, sink: UsageSink):
    if not llm_settings.has_llm_credentials:
        raise RuntimeError(llm_settings.llm_credentials_error)

    if llm_settings.normalized_llm_provider == "azure":
        raw_client = AsyncAzureOpenAI(
            azure_endpoint=llm_settings.azure_openai_endpoint,
            api_key=llm_settings.azure_openai_api_key,
            api_version=llm_settings.azure_openai_api_version,
            timeout=None,
            # The adapter owns retries so quota/auth/config failures can be
            # rejected immediately instead of being retried blindly by the SDK.
            max_retries=0,
        )
    else:
        raw_client = AsyncOpenAI(
            api_key=llm_settings.llm_api_key,
            base_url=llm_settings.llm_base_url,
            timeout=None,
            max_retries=0,
        )

    return LLMClientAdapter(raw_client, llm_settings, sink=sink)
