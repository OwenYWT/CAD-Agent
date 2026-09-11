from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionChunk

from app.config import Settings
from app.llm import ChatCompletionAdapter, get_last_chat_completion_provenance, reset_chat_completion_provenance


class Stream:
    def __init__(self, parts):
        self.parts = iter(parts)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.parts)
        except StopIteration:
            raise StopAsyncIteration

    async def close(self):
        self.closed = True


def chunk(content=None, finish=None, *, identity="completion-1", usage=False):
    return ChatCompletionChunk(id=identity, object="chat.completion.chunk", created=1, model="actual-model",
        choices=[] if usage else [{"index": 0, "delta": {"content": content}, "finish_reason": finish}],
        usage={"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18} if usage else None)


async def adapter_response(parts):
    stream = Stream(parts)
    async def create(**_kwargs):
        return stream
    adapter = ChatCompletionAdapter(SimpleNamespace(create=create), Settings(app_environment="test", llm_max_retries=0))
    reset_chat_completion_provenance()
    result = await adapter.create(model="requested-model", messages=[], stream=True)
    assert stream.closed
    return result


@pytest.mark.asyncio
async def test_stream_preserves_provider_identity_finish_usage_and_exact_content():
    result = await adapter_response([chunk('{"ok":'), chunk('true}'), chunk(finish="stop"), chunk(usage=True)])
    assert result.choices[0].message.content == '{"ok":true}'
    provenance = get_last_chat_completion_provenance()
    assert provenance["provider_response_id"] == "completion-1"
    assert provenance["model"] == "actual-model" and provenance["usage"]["total_tokens"] == 18


@pytest.mark.asyncio
@pytest.mark.parametrize("parts", [[chunk('{}')], [chunk('{'), chunk('}', finish="stop", identity="different")]])
async def test_incomplete_or_mixed_response_stream_cannot_be_reported_as_success(parts):
    with pytest.raises(ValueError, match="stream"):
        await adapter_response(parts)
    assert get_last_chat_completion_provenance() is None


@pytest.mark.asyncio
async def test_stream_preserves_truncation_for_generator_retry():
    result = await adapter_response([chunk('{"partial":', finish="length")])
    assert result.choices[0].finish_reason == "length"
