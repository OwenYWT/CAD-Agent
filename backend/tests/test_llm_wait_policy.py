"""Real loopback HTTP tests of SDK wait/cancellation, not CAD/provider validation."""
import asyncio
import json
import os
import time

import pytest

from app.config import Settings
from app.llm import create_llm_client, get_last_chat_completion_provenance


@pytest.mark.parametrize("provider", ["moonshot", "openai_compatible", "azure"])
@pytest.mark.asyncio
async def test_all_provider_clients_ignore_legacy_wait_limit(monkeypatch, provider):
    monkeypatch.setenv("LLM_TIMEOUT_S", "0.01")
    settings = Settings(
        _env_file=None, llm_provider=provider, moonshot_api_key="test-only",
        dashscope_api_key="test-only", azure_openai_api_key="test-only",
        azure_openai_endpoint="https://example.invalid",
    )
    client = create_llm_client(settings)
    try:
        assert client.timeout is None
        assert client.raw_client._client.timeout.as_dict() == {
            "connect": None, "read": None, "write": None, "pool": None,
        }
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_real_http_wait_survives_caller_timeout_override(stream):
    # Set 65 for the explicit old-60-second-boundary check. Short by default so
    # routine regressions still exercise the socket without taking two minutes.
    delay = float(os.environ.get("CAD_TEST_LLM_WAIT_SECONDS", "0.1"))
    handlers = set()
    requests = []

    async def handle(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            requests.append(json.loads(await reader.readexactly(length)))
            if stream:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
                await writer.drain()
                # No chunks/keepalive during the entire interval.
                await asyncio.sleep(delay)
                chunk = {"id": "transport-test", "object": "chat.completion.chunk",
                         "created": 1, "model": "transport-test", "choices": [
                             {"index": 0, "delta": {"content": "transport-only"}, "finish_reason": "stop"}]}
                writer.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
            else:
                # Wait before headers, as a non-streaming provider can do.
                await asyncio.sleep(delay)
                body = json.dumps({"id": "transport-test", "object": "chat.completion",
                                   "created": 1, "model": "transport-test", "choices": [
                                       {"index": 0, "message": {"role": "assistant", "content": "transport-only"},
                                        "finish_reason": "stop"}]}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = create_llm_client(Settings(_env_file=None, moonshot_api_key="test-only",
                                       llm_base_url=f"http://127.0.0.1:{port}/v1", llm_max_retries=0))
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(client.chat.completions.create(
            model="transport-test", messages=[], stream=stream, timeout=0.01,
        ), timeout=delay + 10)  # Test harness deadline only, not application policy.
        assert time.monotonic() - started >= delay
        assert result.choices[0].message.content == "transport-only"
        assert len(requests) == 1  # No timeout/retry hid a failure.
        assert "timeout" not in requests[0]  # Transport option, not model input.
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*handlers)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_wait_without_deadline_is_cancellable_and_closes_connection(stream):
    received = asyncio.Event()
    disconnected = asyncio.Event()

    async def handle(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            await reader.readexactly(length)
            if stream:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
                await writer.drain()
            received.set()
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()
            disconnected.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = create_llm_client(Settings(_env_file=None, moonshot_api_key="test-only",
                                       llm_base_url=f"http://127.0.0.1:{port}/v1", llm_max_retries=1))
    task = asyncio.create_task(client.chat.completions.create(model="transport-test", messages=[], stream=stream))
    try:
        await asyncio.wait_for(received.wait(), 5)
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await asyncio.wait_for(disconnected.wait(), 5)
        assert get_last_chat_completion_provenance() is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()
        server.close()
        await server.wait_closed()
