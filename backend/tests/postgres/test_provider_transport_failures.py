"""Real sockets, SDK and PostgreSQL; no model generation or CAD success fixture."""
import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
import os
import socket

import httpx
import pytest
from openai import APIConnectionError
from sqlalchemy import text

from app.config import Settings
from app.contracts.provider_budget import ProviderBudgetError, provider_budget_context
from app.db import tenant_transaction
from app.llm import create_llm_client
from app.services import llm_usage, model_jobs as jobs
from app.workflows.errors import planning_error
from tests.postgres.test_model_jobs import request_job
from tests.postgres.test_provider_budgets import accounts, budget_job, reserve

pytestmark = pytest.mark.skipif(not os.getenv('CAD_MODEL_JOB_TEST_DATABASE_URL'),
                              reason='isolated migrated PostgreSQL required')


@asynccontextmanager
async def wire_server(mode, received):
    async def serve(reader, writer):
        try:
            if mode == 'tls_eof':
                received.append(await reader.read(1))  # TLS ClientHello, not HTTP.
            else:
                headers = await reader.readuntil(b'\r\n\r\n')
                size = next(int(h.split(b':', 1)[1]) for h in headers.split(b'\r\n')
                            if h.lower().startswith(b'content-length:'))
                received.append(await reader.readexactly(size))
                if mode == 'wait_for_disconnect':
                    await reader.read()
                if mode == 'stream_eof':
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                                 b'Content-Length: 1000\r\nConnection: close\r\n\r\n'
                                 b'data: {"id":"partial","object":"chat.completion.chunk",'
                                 b'"created":1,"model":"kimi-k2.7-code","choices":[]}\n\n')
                    await writer.drain()
                if mode.startswith('https://'):
                    writer.write(b'HTTP/1.1 307 Temporary Redirect\r\nLocation: '
                                 + mode.encode() + b'\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        yield f"{'https' if mode == 'tls_eof' else 'http'}://127.0.0.1:{port}/v1"


async def invoke(port, endpoint, *, stream=False):
    client = create_llm_client(Settings(_env_file=None, moonshot_api_key='test-secret',
        llm_base_url=endpoint, llm_max_retries=1), sink=llm_usage)
    token = provider_budget_context.set(port)
    try:
        return await client.chat.completions.create(model='kimi-k2.7-code',
            messages=[{'role': 'user', 'content': 'private-test-input'}], stream=stream)
    finally:
        provider_budget_context.reset(token)
        await client.raw_client.close()


async def ledger(job):
    async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
        rows = (await conn.execute(text('SELECT * FROM provider_budget_reservations '
            'WHERE model_job_id=:id ORDER BY attempt'), job)).mappings().all()
        events = (await conn.execute(text('SELECT e.* FROM provider_budget_events e '
            'JOIN provider_budget_reservations r ON r.id=e.reservation_id '
            'WHERE r.model_job_id=:id ORDER BY e.created_at'), job)).mappings().all()
        return rows, events


@pytest.mark.asyncio
async def test_tls_failure_before_http_releases_only_money_and_preserves_diagnostics(budget_job):
    job, port = budget_job
    received = []
    async with wire_server('tls_eof', received) as endpoint:
        with pytest.raises(APIConnectionError) as failure:
            await invoke(port, endpoint)
    assert len(received) == 2 and all(p == b'\x16' for p in received)
    rows, events = await ledger(job)
    assert [r['status'] for r in rows] == ['released_unsent', 'released_unsent']
    assert all(a['held'] == 0 and a['settled'] == 0 and a['requests_used'] == 2
               for a in await accounts(job))
    diagnostics = list(planning_error(failure.value).details)[0]['provider_attempts']
    assert len(diagnostics) == 2
    assert all(d['transport']['failure_phase'] == 'connection.start_tls.failed' for d in diagnostics)
    assert all(d['transport']['request_headers_started'] is False for d in diagnostics)
    assert any(e['event_type'] == 'released_unsent' and e['evidence'].get('diagnostics') for e in events)
    assert 'test-secret' not in str(diagnostics) and 'private-test-input' not in str(diagnostics)


@pytest.mark.asyncio
async def test_reply_lost_after_send_retains_money_and_original_error_in_saved_job(budget_job):
    job, port = budget_job
    received = []
    async with wire_server('http_eof', received) as endpoint:
        async def operation(_):
            try:
                await invoke(port, endpoint)
            except Exception as exc:
                raise planning_error(exc) from exc
        await jobs.execute(job, {job['operation']: operation})
    assert len(received) == 1
    rows, events = await ledger(job)
    assert len(rows) == 1 and rows[0]['status'] == 'unknown'
    assert all(a['held'] == Decimal('2.588672') and a['settled'] == 0 for a in await accounts(job))
    async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
        saved = (await conn.execute(text('SELECT error FROM model_jobs WHERE id=:id'), job)).scalar_one()
    assert saved['code'] == 'provider_budget_exhausted'
    assert 'APIConnectionError' in saved['message']
    details = saved['details'][0]
    assert details['retry_blocked']['code'] == 'provider_budget_exhausted'
    assert details['provider_attempts'][0]['transport']['request_headers_started'] is True
    assert details['provider_attempts'][0]['transport']['failure_phase'] == 'http11.receive_response_headers.failed'
    assert 'RemoteProtocolError' in str(details['provider_attempts'][0]['cause_chain'])
    assert any(e['event_type'] == 'unknown' and e['evidence'].get('diagnostics') for e in events)
    assert 'test-secret' not in str(details) and 'private-test-input' not in str(details)


@pytest.mark.asyncio
async def test_tls_error_after_redirect_cannot_release_original_send(budget_job):
    job, port = budget_job
    sent, handshakes = [], []
    async with wire_server('tls_eof', handshakes) as target:
        async with wire_server(target + '/chat/completions', sent) as source:
            with pytest.raises(ProviderBudgetError) as failure:
                await invoke(port, source)
    assert len(sent) == len(handshakes) == 1
    rows, _ = await ledger(job)
    assert len(rows) == 1 and rows[0]['status'] == 'unknown'
    assert all(a['held'] == Decimal('2.588672') for a in await accounts(job))
    diagnostic = list(planning_error(failure.value).details)[0]['provider_attempts'][0]
    assert diagnostic['transport']['request_headers_started'] is True
    assert diagnostic['transport']['failure_phase'] == 'connection.start_tls.failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', [
    {},
    {'request_headers_started': False},
    {'failure_phase': 'connection.start_tls.failed'},
    {'failure_phase': 'connection.start_tls.failed', 'request_headers_started': True},
    {'failure_phase': 'http11.receive_response_headers.failed', 'request_headers_started': False},
])
async def test_missing_or_ambiguous_transport_evidence_never_refunds(budget_job, transport):
    job, port = budget_job
    row = await reserve(port)
    await port.claim_dispatch(row)
    with pytest.raises(ProviderBudgetError, match='unproven refund'):
        await port.release_unsent(row, diagnostics={'transport': transport})
    assert all(a['held'] == Decimal('2.588672') for a in await accounts(job))


@pytest.mark.asyncio
async def test_transport_evidence_cannot_rewrite_unknown_history(budget_job):
    job, port = budget_job
    row = await reserve(port)
    await port.claim_dispatch(row)
    await port.mark_unknown(row, reason='historical_unobserved_connection_error')
    with pytest.raises(ProviderBudgetError, match='unproven refund'):
        await port.release_unsent(row, diagnostics={'transport': {
            'failure_phase': 'connection.start_tls.failed', 'request_headers_started': False}})
    assert all(a['held'] == Decimal('2.588672') for a in await accounts(job))


@pytest.mark.asyncio
async def test_proven_unsent_release_is_atomic_and_idempotent(budget_job):
    job, port = budget_job
    row = await reserve(port)
    await port.claim_dispatch(row)
    diagnostic = {'transport': {'failure_phase': 'connection.connect_tcp.failed',
                                'request_headers_started': False}}
    await asyncio.gather(*(port.release_unsent(row, diagnostics=diagnostic) for _ in range(3)))
    assert all(a['held'] == 0 and a['requests_used'] == 1 for a in await accounts(job))
    with pytest.raises(ProviderBudgetError):
        await port.claim_dispatch(row)
    rows, events = await ledger(job)
    assert rows[0]['status'] == 'released_unsent'
    assert sum(e['event_type'] == 'released_unsent' for e in events) == 1


@pytest.mark.asyncio
async def test_tcp_refused_without_any_http_send_can_retry(budget_job):
    job, port = budget_job
    with socket.socket() as endpoint:
        endpoint.bind(('127.0.0.1', 0))  # Reserved but not listening: real ECONNREFUSED.
        with pytest.raises(APIConnectionError):
            await invoke(port, f'http://127.0.0.1:{endpoint.getsockname()[1]}/v1')
    rows, events = await ledger(job)
    assert len(rows) == 2 and all(r['status'] == 'released_unsent' for r in rows)
    assert all(a['held'] == 0 and a['requests_used'] == 2 for a in await accounts(job))
    assert all(e['evidence']['diagnostics']['transport']['failure_phase'] == 'connection.connect_tcp.failed'
               for e in events if e['event_type'] == 'released_unsent')


@pytest.mark.asyncio
async def test_stream_disconnect_keeps_unsettled_money(budget_job):
    job, port = budget_job
    received = []
    async with wire_server('stream_eof', received) as endpoint:
        with pytest.raises(httpx.RemoteProtocolError):
            await invoke(port, endpoint, stream=True)
    rows, _ = await ledger(job)
    assert len(received) == len(rows) == 1 and rows[0]['status'] == 'unknown'
    assert all(a['held'] == Decimal('2.588672') for a in await accounts(job))


@pytest.mark.asyncio
async def test_cancel_after_http_send_does_not_release_money(budget_job):
    job, port = budget_job
    received = []
    async with wire_server('wait_for_disconnect', received) as endpoint:
        task = asyncio.create_task(invoke(port, endpoint))
        async def wait_received():
            while not received:
                await asyncio.sleep(0.01)
        try:
            await asyncio.wait_for(wait_received(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    rows, _ = await ledger(job)
    assert len(rows) == 1 and rows[0]['status'] == 'unknown'
    assert all(a['held'] == Decimal('2.588672') for a in await accounts(job))
