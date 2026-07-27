"""Reliability behaviors: overall deadline, sessions LRU bound, request-id logging,
LLM client factory timeouts. All hermetic (no Docker, no LLM)."""
import asyncio
import logging

import pytest

from app.config import settings, make_llm_client
from app.logging_context import set_request_id, get_request_id, RequestIdFilter


# === request-id logging correlation (#21) ===

def test_request_id_contextvar_roundtrip():
    set_request_id("req-xyz")
    assert get_request_id() == "req-xyz"


def test_request_id_filter_injects_attr():
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "msg", None, None)
    set_request_id("req-abc")
    assert RequestIdFilter().filter(rec) is True
    assert rec.request_id == "req-abc"


# === sessions LRU bound (#18) ===

def test_sessions_lru_eviction(monkeypatch):
    from app.api import websocket
    monkeypatch.setattr(websocket, "_MAX_SESSIONS", 3)
    websocket.sessions.clear()
    for i in range(5):
        websocket._get_context(f"s{i}", "default")
    # only the last 3 survive; oldest two evicted
    assert len(websocket.sessions) == 3
    assert "s0" not in websocket.sessions and "s1" not in websocket.sessions
    assert "s4" in websocket.sessions


def test_sessions_lru_marks_recent(monkeypatch):
    from app.api import websocket
    monkeypatch.setattr(websocket, "_MAX_SESSIONS", 3)
    websocket.sessions.clear()
    for i in range(3):
        websocket._get_context(f"s{i}", "default")
    websocket._get_context("s0", "default")   # touch s0 → now most-recent
    websocket._get_context("s3", "default")   # push over cap → evicts s1 (oldest), not s0
    assert "s0" in websocket.sessions
    assert "s1" not in websocket.sessions


# === LLM client factory timeouts (#17/#23) ===

def test_llm_factory_requires_key(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "moonshot")
    monkeypatch.setattr(settings, "moonshot_api_key", None)
    with pytest.raises(RuntimeError, match="MOONSHOT_API_KEY"):
        make_llm_client()


def test_llm_factory_sets_timeout(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "moonshot")
    monkeypatch.setattr(settings, "moonshot_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_timeout_s", 42.0)
    client = make_llm_client()
    assert client.timeout == 42.0


# === planner no longer masks unexpected exceptions (#26) ===

@pytest.mark.asyncio
async def test_planner_reraises_missing_credentials(monkeypatch):
    """Missing-key RuntimeError must propagate, not silently fall back to a generic plan."""
    monkeypatch.setattr(settings, "llm_provider", "moonshot")
    monkeypatch.setattr(settings, "moonshot_api_key", None)
    from app.agent.planner import Planner
    p = Planner()
    with pytest.raises(RuntimeError, match="MOONSHOT_API_KEY"):
        await p.plan_new([{"role": "user", "content": "一个盒子"}])

@pytest.mark.asyncio
async def test_planner_empty_output_fails_closed():
    """Empty model output must not become a fabricated generic CAD plan."""
    from types import SimpleNamespace
    from app.agent.planner import Planner

    class EmptyCompletions:
        def __init__(self):
            self.calls = 0

        async def create(self, **_kwargs):
            self.calls += 1
            message = SimpleNamespace(content=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    completions = EmptyCompletions()
    planner = Planner()
    planner._client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )

    with pytest.raises(ValueError, match="valid CAD plan"):
        await planner.plan_new([{"role": "user", "content": "一个盒子"}])
    assert completions.calls == 2


@pytest.mark.asyncio
async def test_planner_retries_truncated_output_with_configured_budget(monkeypatch):
    """A length-limited response is never parsed as a plan; retry stays model-backed."""
    from types import SimpleNamespace
    from app.agent.planner import Planner

    valid = (
        '{"description":"盒子","part_type":"box","dimensions":{"width":20,'
        '"height":10,"depth":5},"features":[]}'
    )

    class RecordingCompletions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(content='{"description":"截断'),
                )])
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=valid),
            )])

    monkeypatch.setattr(settings, "planner_max_tokens", 8192)
    completions = RecordingCompletions()
    planner = Planner()
    planner._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    plan = await planner.plan_new([{"role": "user", "content": "一个盒子"}])

    assert plan.description == "盒子"
    assert len(completions.calls) == 2
    assert all(call["max_tokens"] == 8192 for call in completions.calls)


@pytest.mark.asyncio
async def test_planner_does_not_retry_nonrecoverable_provider_quota():
    import httpx
    from openai import RateLimitError
    from app.agent.planner import Planner

    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(429, request=request)

    class QuotaCompletions:
        def __init__(self):
            self.calls = 0

        async def create(self, **_kwargs):
            self.calls += 1
            raise RateLimitError(
                "insufficient balance",
                response=response,
                body={"error": {"type": "exceeded_current_quota_error"}},
            )

    completions = QuotaCompletions()
    planner = Planner()
    planner._client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": completions})()},
    )()

    with pytest.raises(RateLimitError):
        await planner.plan_new([{"role": "user", "content": "一个盒子"}])
    assert completions.calls == 1


# === overall deadline (#17) at the REST layer ===

@pytest.mark.asyncio
async def test_generate_deadline_returns_504(monkeypatch):
    """If the pipeline exceeds generate_deadline_s, the endpoint returns 504, not a hang."""
    from app.api import websocket, generate as generate_api
    from fastapi import Request

    class SlowOrch:
        async def generate(self, prompt, output_formats):
            await asyncio.sleep(5)  # longer than the patched deadline

    monkeypatch.setattr(generate_api, "_get_orchestrator", lambda: SlowOrch())
    monkeypatch.setattr(settings, "generate_deadline_s", 0.1)

    # minimal Request stub for rate_limiter._client_key
    scope = {"type": "http", "client": ("1.2.3.4", 1234), "headers": []}
    req = Request(scope)

    from app.models.schemas import GenerateRequest
    resp = await generate_api.generate(GenerateRequest(prompt="x"), req, api_key=None)
    assert resp.status_code == 504
    import json
    body = json.loads(resp.body)
    assert body["error"]["type"] == "TimeoutError"
