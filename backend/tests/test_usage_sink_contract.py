"""Fault injection at the real telemetry adapter, not a production no-op sink."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app.config import Settings
from app.llm import ChatCompletionAdapter
from app.services import llm_usage

@pytest.mark.asyncio
async def test_usage_storage_failure_does_not_replace_provider_result(monkeypatch, caplog):
    @asynccontextmanager
    async def unavailable(*args, **kwargs):
        raise OSError('injected telemetry storage outage')
        yield
    monkeypatch.setattr(llm_usage, 'tenant_transaction', unavailable)
    monkeypatch.setattr(llm_usage.settings, 'durable_control_plane_enabled', True)
    context = llm_usage.UsageContext(uuid4(), uuid4())
    token = llm_usage.usage_context.set(context)
    response = SimpleNamespace(id='controlled-response', model='controlled-model', usage=None,
        choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='controlled result'))])
    class ControlledProvider:
        async def create(self, **params):
            assert params['timeout'] is None
            return response
    try:
        adapter = ChatCompletionAdapter(ControlledProvider(), Settings(_env_file=None), sink=llm_usage)
        assert await adapter.create(model='controlled-model', messages=[]) is response
        # Also exercise a storage failure after a call already has a valid handle.
        await llm_usage.finish_call((uuid4(), context), duration_ms=1,
                                   provenance={'finish_reason':'stop'})
        assert 'LLM_USAGE_WRITE_FAILED: start' in caplog.text
        assert 'LLM_USAGE_WRITE_FAILED: finish' in caplog.text
    finally:
        llm_usage.usage_context.reset(token)
