"""Non-generating provider checks exercise the real SDK and local HTTP sockets."""
import asyncio
import json

import httpx
from fastapi import FastAPI
import pytest

from app.api.auth import verify_api_key
from app.api.generate import router
from app.config import Settings, settings
from app.llm import create_llm_client
from app.services import llm_usage


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 401])
async def test_provider_preflight_uses_catalog_and_never_generation(monkeypatch, status):
    received = []
    async def serve(reader, writer):
        try:
            received.append(await reader.readuntil(b'\r\n\r\n'))
            body = json.dumps({'object': 'list', 'data': [{'id': 'configured-model', 'object': 'model'}]}
                              if status == 200 else {'error': {'message': 'secret-provider-detail'}}).encode()
            writer.write(f'HTTP/1.1 {status} Reply\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(settings, 'llm_base_url', f'http://127.0.0.1:{port}/v1')
    monkeypatch.setattr(settings, 'moonshot_api_key', 'private-test-key')
    monkeypatch.setattr(settings, 'llm_model', 'configured-model')
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_api_key] = lambda: 'local-probe-test'
    async with server, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/provider/preflight')
    assert len(received) == 1 and received[0].startswith(b'GET /v1/models ')
    assert 'private-test-key' not in response.text and 'secret-provider-detail' not in response.text
    assert response.json()['scope'] == 'api_process'
    if status == 200:
        assert response.status_code == 200
        assert response.json()['status'] == 'ready'
        assert response.json()['model_listed'] is True
        assert response.json()['generation_tested'] is False
    else:
        assert response.status_code == 503
        assert response.json()['error']['type'] == 'ProviderAuthenticationError'
        assert response.json()['details']['provider_attempts'][0]['http_status'] == 401


@pytest.mark.asyncio
@pytest.mark.auth
async def test_provider_preflight_requires_authentication(monkeypatch):
    monkeypatch.setattr(settings, 'auth_required', True)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/provider/preflight')
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_parallel_provider_probes_keep_transport_evidence_separate():
    from tests.postgres.test_provider_transport_failures import wire_server
    tls = []
    async with wire_server('tls_eof', tls) as endpoint:
        client = create_llm_client(Settings(_env_file=None, moonshot_api_key='test-only',
            llm_base_url=endpoint, llm_max_retries=0), sink=llm_usage)
        try:
            outcomes = await asyncio.gather(client.check_connection(), client.check_connection(), return_exceptions=True)
        finally:
            await client.raw_client.close()
    assert len(tls) == 2
    for outcome in outcomes:
        diagnostic = outcome.provider_diagnostics['provider_attempts'][0]
        assert diagnostic['attempt'] == 1
        assert diagnostic['transport']['failure_phase'] == 'connection.start_tls.failed'
        assert diagnostic['transport']['request_headers_started'] is False
