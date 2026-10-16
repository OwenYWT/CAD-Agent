"""Real PostgreSQL admission, concurrency, fencing and receipt recovery."""
import asyncio
from contextlib import asynccontextmanager
import json
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.contracts.provider_budget import ProviderBudgetError
from app.db import tenant_transaction
from app.services import model_jobs as jobs
from app.services.provider_budgets import PgProviderBudget, admit, freeze_policy
from tests.postgres.test_model_jobs import request_job, pytestmark  # noqa: F401
from tests.test_provider_budget_policy import cost_contract


@pytest_asyncio.fixture
async def budget_job(request_job):
    p = request_job['payload']
    policy = freeze_policy(mode='hard_cost', request_limit=8, month_request_limit=20, cost_contract=cost_contract())
    async with tenant_transaction(UUID(p['tenant_id']), UUID(p['principal_id'])) as conn:
        await admit(conn, tenant_id=UUID(p['tenant_id']), workflow_id=UUID(p['workflow_run_id']), policy=policy)
    await jobs.submit(request_job)
    job = await jobs.claim()
    assert str(job['id']) == request_job['job_id']
    port = PgProviderBudget(job)
    await port.prepare_request({'model': 'kimi-k2.7-code', 'messages': []}, provider='moonshot')
    return job, port


async def reserve(port, logical='first', attempt=1, digest='digest'):
    return await port.reserve_attempt(logical_call_id=logical, attempt=attempt,
                                     request_digest=digest, provider='moonshot', model='kimi-k2.7-code')


async def accounts(job):
    async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
        return (await conn.execute(text('SELECT * FROM provider_budget_accounts ORDER BY scope'))).mappings().all()


def receipt(**values):
    return {'provider': 'moonshot', 'model': 'kimi-k2.7-code', 'request_model': 'kimi-k2.7-code',
            'request_hash': 'digest', **values}


@pytest.mark.asyncio
async def test_simultaneous_admission_honors_shared_currency_limit(budget_job):
    job, port = budget_job
    results = await asyncio.gather(*(reserve(port, str(i)) for i in range(4)), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert all(isinstance(r, dict) or (isinstance(r, ProviderBudgetError) and r.code == 'provider_budget_exhausted') for r in results)
    assert all(a['requests_used'] == 1 and a['held'] == Decimal('2.588672') for a in await accounts(job))


@pytest.mark.asyncio
async def test_logical_identity_claim_and_receipt_are_idempotent(budget_job):
    job, port = budget_job
    row = await reserve(port)
    assert (await reserve(port))['id'] == row['id']
    with pytest.raises(ProviderBudgetError, match='Logical request changed'):
        await reserve(port, digest='changed')
    assert await port.claim_dispatch(row) is None
    with pytest.raises(ProviderBudgetError, match='cannot be sent again'):
        await port.claim_dispatch(row)
    proof = receipt(usage={'prompt_tokens': 1, 'completion_tokens': 1}, provider_response_id='response-1')
    response = {'actual_response': 'retained exactly'}
    await port.settle(row, provenance=proof, response=response)
    await port.settle(row, provenance=proof, response=response)
    assert await port.claim_dispatch(row) == response
    assert all(a['held'] == 0 and a['settled'] == Decimal('0.0000335') for a in await accounts(job))
    # Equal content can be an intentional new call; digest is not its identity.
    other = await reserve(port, 'second')
    assert other['id'] != row['id']
    with pytest.raises(ProviderBudgetError, match='different receipt'):
        await port.settle(row, provenance={'usage': {'prompt_tokens': 2, 'completion_tokens': 2}}, response=response)


@pytest.mark.asyncio
@pytest.mark.parametrize('with_usage', [True, False])
async def test_completed_reply_replayed_after_worker_takeover(budget_job, with_usage):
    job, port = budget_job
    row = await reserve(port)
    await port.claim_dispatch(row)
    proof = receipt(usage={'prompt_tokens': 1, 'completion_tokens': 1}) if with_usage else receipt()
    response = {'retained_reply': 'returned before worker shutdown'}
    await port.settle(row, provenance=proof, response=response)
    before = await accounts(job)
    async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
        await conn.execute(text("UPDATE model_jobs SET lease_until=now()-interval '1 second' WHERE id=:id"), job)
    recovered = await jobs.claim()
    assert recovered['generation'] == job['generation'] + 1
    newer = PgProviderBudget(recovered)
    await newer.prepare_request({'model': 'kimi-k2.7-code', 'messages': []}, provider='moonshot')
    replay = await reserve(newer)
    assert replay['id'] == row['id']
    assert await newer.claim_dispatch(replay) == response
    assert await accounts(job) == before
    with pytest.raises(asyncio.CancelledError):
        await port.claim_dispatch(row)
    # An identical ordinal with changed input is a new physical attempt, not a
    # license to substitute a response for a different request.
    if with_usage:
        changed = await reserve(newer, digest='different-request')
        assert changed['id'] != row['id']
        assert await newer.claim_dispatch(changed) is None
    else:
        with pytest.raises(ProviderBudgetError, match='exhausted'):
            await reserve(newer, digest='different-request')


@pytest.mark.asyncio
async def test_missing_usage_holds_money_and_late_receipt_survives_takeover(budget_job):
    job, port = budget_job
    row = await reserve(port)
    await port.claim_dispatch(row)
    await port.settle(row, provenance=receipt(), response={'retained': True})
    assert all(a['held'] == Decimal('2.588672') and a['settled'] == 0 for a in await accounts(job))
    async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
        await conn.execute(text("UPDATE model_jobs SET lease_until=now()-interval '1 second' WHERE id=:id"), job)
    new = await jobs.claim()
    assert new['generation'] == job['generation'] + 1
    with pytest.raises(asyncio.CancelledError):
        await reserve(port, 'stale')
    await port.settle(row, provenance=receipt(usage={'prompt_tokens': 100, 'completion_tokens': 10}), response={'retained': True})
    assert all(a['held'] == 0 and a['settled'] == Decimal('0.00092') for a in await accounts(job))
    newer = PgProviderBudget(new)
    await newer.prepare_request({'model': 'kimi-k2.7-code', 'messages': []}, provider='moonshot')
    replay = await reserve(newer)
    assert replay['id'] == row['id']
    assert await newer.claim_dispatch(replay) == {'retained': True}


@pytest.mark.asyncio
async def test_unknown_attempt_cannot_be_refunded_on_lease_or_timeout(budget_job):
    job, port = budget_job
    row = await reserve(port)
    await port.claim_dispatch(row)
    await port.mark_unknown(row, reason='connection_lost')
    with pytest.raises(ProviderBudgetError, match='unproven refund'):
        await port.release_unsent(row)
    with pytest.raises(ProviderBudgetError, match='exhausted'):
        await reserve(port, attempt=2)
    assert all(a['held'] == Decimal('2.588672') for a in await accounts(job))


@pytest.mark.asyncio
async def test_only_never_claimed_dispatch_can_release_and_count_is_retained(budget_job):
    job, port = budget_job
    row = await reserve(port)
    await port.release_unsent(row)
    await port.release_unsent(row)
    assert all(a['held'] == 0 and a['requests_used'] == 1 for a in await accounts(job))
    with pytest.raises(ProviderBudgetError):
        await port.claim_dispatch(row)


@pytest.mark.asyncio
async def test_over_bound_usage_is_recorded_and_freezes_accounts(budget_job):
    job, port = budget_job
    row = await reserve(port)
    await port.claim_dispatch(row)
    await port.settle(row, provenance=receipt(usage={'prompt_tokens': 500000, 'completion_tokens': 100000}), response={})
    assert all(a['settled'] == Decimal('5.95') and a['frozen'] and a['held'] == 0 for a in await accounts(job))
    with pytest.raises(ProviderBudgetError, match='exhausted'):
        await reserve(port, 'second')


@pytest.mark.asyncio
async def test_tenant_month_is_shared_across_tasks_and_rls_isolation(budget_job):
    from app.domain.identity import user_principal
    from app.repositories.identity import reconcile_principal
    job, port = budget_job
    await reserve(port)
    from app.services.run_state import create_workflow
    async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
        workflow = await conn.scalar(text('SELECT project_id FROM workflow_runs WHERE id=:id'), {'id': job['workflow_run_id']})
        run = await create_workflow(conn, tenant_id=job['tenant_id'], project_id=workflow,
            requested_by_principal_id=job['principal_id'], kind='mcad.agent.v2.generate',
            idempotency_key=str(uuid4()), request_payload={})
        await admit(conn, tenant_id=job['tenant_id'], workflow_id=run.workflow_id, policy=port.policy)
    request = {'job_id': str(uuid4()), 'operation': job['operation'],
        'payload': {**job['payload'], 'workflow_run_id': str(run.workflow_id)}}
    await jobs.submit(request)
    other_job = await jobs.claim()
    other = PgProviderBudget(other_job)
    await other.prepare_request({'model': 'kimi-k2.7-code', 'messages': []}, provider='moonshot')
    with pytest.raises(ProviderBudgetError, match='exhausted'):
        await reserve(other)
    outsider = user_principal(str(uuid4())); await reconcile_principal(outsider)
    async with tenant_transaction(outsider.tenant_id, outsider.principal_id) as conn:
        assert await conn.scalar(text('SELECT count(*) FROM provider_budget_reservations')) == 0
    await jobs.finish(other_job, status='succeeded', result={})


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('request_hash','other'), ('provider','other'), ('request_model','other')])
async def test_wrong_request_receipt_cannot_release_reservation(budget_job, field, value):
    job, port = budget_job
    row = await reserve(port)
    await port.claim_dispatch(row)
    with pytest.raises(ProviderBudgetError, match='different provider request'):
        await port.settle(row, provenance=receipt(**{field:value}, usage={'prompt_tokens':1,'completion_tokens':1}), response={})
    assert all(a['held'] == Decimal('2.588672') and a['settled'] == 0 for a in await accounts(job))


@pytest.mark.asyncio
async def test_unpriced_response_model_keeps_full_reservation(budget_job):
    job, port = budget_job
    row = await reserve(port)
    await port.claim_dispatch(row)
    await port.settle(row, provenance=receipt(model='different-priced-model', usage={'prompt_tokens':1,'completion_tokens':1}), response={})
    assert all(a['held'] == Decimal('2.588672') and a['settled'] == 0 for a in await accounts(job))


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['usage_log', 'reservation_database', 'settlement_database', 'worker_takeover'])
async def test_real_http_dispatch_is_independent_of_usage_log_and_fails_closed(request_job, monkeypatch, failure):
    """A transport fixture, not a model/CAD success fixture; database and sockets are real."""
    from app.config import Settings
    from app.contracts.provider_budget import provider_budget_context
    from app.llm import create_llm_client
    from app.services import llm_usage
    p=request_job['payload']
    policy=freeze_policy(mode='hard_cost',request_limit=1,month_request_limit=10,cost_contract=cost_contract())
    async with tenant_transaction(UUID(p['tenant_id']),UUID(p['principal_id'])) as conn:
        await admit(conn,tenant_id=UUID(p['tenant_id']),workflow_id=UUID(p['workflow_run_id']),policy=policy)
    await jobs.submit(request_job)
    job=await jobs.claim()
    budget=PgProviderBudget(job)
    dispatched=[]
    async def serve(reader,writer):
        try:
            headers=await reader.readuntil(b'\r\n\r\n')
            size=next(int(x.split(b':',1)[1]) for x in headers.split(b'\r\n') if x.lower().startswith(b'content-length:'))
            dispatched.append(json.loads(await reader.readexactly(size)))
            body=json.dumps({'id':'transport-receipt','object':'chat.completion','created':1,'model':'kimi-k2.7-code',
                'choices':[{'index':0,'message':{'role':'assistant','content':'transport-only'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
            await writer.drain()
        finally:
            writer.close(); await writer.wait_closed()
    async def unavailable(*args,**kwargs):
        raise ConnectionError('controlled budget database outage')
    async def lost_usage(*args,**kwargs):
        return None  # The existing best-effort sink's documented failure result.
    if failure=='reservation_database':
        monkeypatch.setattr(budget,'reserve_attempt',unavailable)
    elif failure=='settlement_database':
        monkeypatch.setattr(budget,'settle',unavailable)
    elif failure=='usage_log':
        monkeypatch.setattr(llm_usage,'start_call',lost_usage)
    server=await asyncio.start_server(serve,'127.0.0.1',0)
    port=server.sockets[0].getsockname()[1]
    client=create_llm_client(Settings(_env_file=None,moonshot_api_key='test-only',llm_max_retries=0,
        llm_base_url=f'http://127.0.0.1:{port}/v1'),sink=llm_usage)
    token=provider_budget_context.set(budget)
    try:
        if failure=='reservation_database':
            with pytest.raises(ConnectionError):
                await client.chat.completions.create(model='kimi-k2.7-code',messages=[])
            assert dispatched==[] and await accounts(job)==[]
        else:
            response=await client.chat.completions.create(model='kimi-k2.7-code',messages=[])
            assert response.choices[0].message.content=='transport-only' and len(dispatched)==1
            if failure=='worker_takeover':
                before=await accounts(job)
                async with tenant_transaction(job['tenant_id'],job['principal_id']) as conn:
                    await conn.execute(text("UPDATE model_jobs SET lease_until=now()-interval '1 second' WHERE id=:id"),job)
                recovered=await jobs.claim()
                assert recovered['generation']==job['generation']+1
                provider_budget_context.set(PgProviderBudget(recovered))
                replay=await client.chat.completions.create(model='kimi-k2.7-code',messages=[])
                assert replay==response and len(dispatched)==1
                assert await accounts(job)==before
            with pytest.raises(ProviderBudgetError,match='exhausted'):
                await client.chat.completions.create(model='kimi-k2.7-code',messages=[])
            assert len(dispatched)==1 and dispatched[0]['max_tokens']==32768
            balances=await accounts(job)
            assert all(a['requests_used']==1 for a in balances)
            if failure=='settlement_database':
                assert all(a['held']==Decimal('2.588672') and a['settled']==0 for a in balances)
                async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
                    pending = (await conn.execute(text('SELECT * FROM provider_budget_reservations '
                        'WHERE model_job_id=:id'), job)).mappings().one()
                assert pending['receipt'] and pending['response'] == response.model_dump(mode='json')
                assert pending['settlement_next_attempt_at'] is not None
            else:
                assert all(a['held']==0 and a['settled']==Decimal('0.0000335') for a in balances)
    finally:
        provider_budget_context.reset(token)
        await client.close(); server.close(); await server.wait_closed()


@asynccontextmanager
async def reject_ledger_write(job, stage):
    """Inject a real PostgreSQL transaction error, not a replacement ledger."""
    from app.db import get_database_engine
    table, condition = {
        'settlement': ('provider_budget_accounts', 'NEW.held < OLD.held'),
        'receipt': ('provider_budget_reservations', 'NEW.receipt IS NOT NULL'),
    }[stage]
    name = 'receipt_fault_' + uuid4().hex
    async with get_database_engine().begin() as conn:
        await conn.execute(text(f'''CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.tenant_id='{job['tenant_id']}'::uuid AND ({condition}) THEN
                RAISE EXCEPTION 'controlled ledger transaction failure';
            END IF; RETURN NEW; END $$'''))
        await conn.execute(text(f'CREATE TRIGGER {name} BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION {name}()'))
    try:
        yield
    finally:
        async with get_database_engine().begin() as conn:
            await conn.execute(text(f'DROP TRIGGER {name} ON {table}'))
            await conn.execute(text(f'DROP FUNCTION {name}()'))


@pytest_asyncio.fixture
async def receipt_transport():
    from app.config import Settings
    from app.llm import create_llm_client
    from app.services import llm_usage
    dispatched = []
    async def serve(reader, writer):
        try:
            headers = await reader.readuntil(b'\r\n\r\n')
            size = next(int(x.split(b':',1)[1]) for x in headers.split(b'\r\n') if x.lower().startswith(b'content-length:'))
            dispatched.append(json.loads(await reader.readexactly(size)))
            body = json.dumps({'id':'durable-receipt', 'object':'chat.completion', 'created':1, 'model':'kimi-k2.7-code',
                'choices':[{'index':0,'message':{'role':'assistant','content':'transport-only'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
            await writer.drain()
        finally:
            writer.close(); await writer.wait_closed()
    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    client = create_llm_client(Settings(_env_file=None, moonshot_api_key='test-only', llm_max_retries=0,
        llm_base_url=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1'), sink=llm_usage)
    try:
        yield client, dispatched
    finally:
        await client.close(); server.close(); await server.wait_closed()


async def reservation_for(job):
    async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
        return (await conn.execute(text('SELECT * FROM provider_budget_reservations WHERE model_job_id=:id'), job)).mappings().one()


@pytest.mark.asyncio
async def test_sql_settlement_failure_recovers_after_completed_job_and_worker_restart(budget_job, request_job, receipt_transport):
    from app.db import close_database
    from app.services.provider_budgets import recover_next_settlement
    job, _ = budget_job
    client, dispatched = receipt_transport
    async def operation(payload):
        return (await client.chat.completions.create(model='kimi-k2.7-code', messages=[])).model_dump(mode='json')
    async with reject_ledger_write(job, 'settlement'):
        await jobs.execute(job, {job['operation']:operation})
        result = await jobs.read(request_job)
        assert result['status'] == 'succeeded' and len(dispatched) == 1
        pending = await reservation_for(job)
        assert pending['status'] == 'dispatch_claimed' and pending['response'] == result['result']
        assert pending['receipt']['provider_response_id'] == 'durable-receipt'
        assert pending['settlement_next_attempt_at'] is not None
        assert all(a['held'] == Decimal('2.588672') and a['settled'] == 0 for a in await accounts(job))
        # A second failure belongs to the durable queue and survives restart.
        for _ in range(10):
            await recover_next_settlement()
            if (await reservation_for(job))['settlement_attempts']:
                break
        failed = await reservation_for(job)
        assert failed['settlement_attempts'] == 1 and failed['settlement_last_error']
        assert failed['settlement_next_attempt_at'] > failed['updated_at']
    async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
        await conn.execute(text("UPDATE provider_budget_reservations SET settlement_next_attempt_at=now()-interval '1 second' WHERE id=:id"), {'id':pending['id']})
    await close_database()
    # The normal worker starts recovery even with no executable Model Jobs.
    worker = asyncio.create_task(jobs.run_model_jobs({}, concurrency=0))
    try:
        async with asyncio.timeout(10):
            while (await reservation_for(job))['status'] != 'settled':
                await asyncio.sleep(.05)
    finally:
        worker.cancel(); await asyncio.gather(worker, return_exceptions=True)
    settled = await reservation_for(job)
    assert settled['settlement_attempts'] == 2 and settled['settlement_next_attempt_at'] is None
    assert settled['settlement_last_error'] is None and len(dispatched) == 1
    assert await jobs.read(request_job) == result
    assert all(a['held'] == 0 and a['settled'] == Decimal('0.0000335') and a['requests_used'] == 1 for a in await accounts(job))
    async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
        assert await conn.scalar(text("SELECT count(*) FROM provider_budget_events WHERE reservation_id=:id AND event_type='settled'"), {'id':pending['id']}) == 1


@pytest.mark.asyncio
async def test_unpersisted_receipt_cannot_report_model_job_success(budget_job, request_job, receipt_transport):
    job, _ = budget_job
    client, dispatched = receipt_transport
    async def operation(payload):
        return (await client.chat.completions.create(model='kimi-k2.7-code', messages=[])).model_dump(mode='json')
    async with reject_ledger_write(job, 'receipt'):
        await jobs.execute(job, {job['operation']:operation})
    assert (await jobs.read(request_job))['status'] == 'failed'
    assert len(dispatched) == 1  # Database failure never resends the provider call.
    row = await reservation_for(job)
    assert row['status'] == 'unknown' and row['receipt'] is None and row['response'] is None
    assert all(a['held'] == Decimal('2.588672') and a['settled'] == 0 for a in await accounts(job))


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['takeover', 'cancelled'])
async def test_pending_receipt_survives_fencing_and_settles_exactly_once(budget_job, request_job, terminal):
    job, port = budget_job
    row = await reserve(port)
    await port.claim_dispatch(row)
    proof = receipt(usage={'prompt_tokens':1,'completion_tokens':1})
    response = {'exact_reply': 1.2345678901234567e20, 'tiny': 5e-324}
    await port.record_receipt(row, provenance=proof, response=response)
    await port.record_receipt(row, provenance=proof, response=response)
    with pytest.raises(ProviderBudgetError, match='different receipt'):
        await port.record_receipt(row, provenance=proof, response={'changed': True})
    if terminal == 'takeover':
        async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
            await conn.execute(text("UPDATE model_jobs SET lease_until=now()-interval '1 second' WHERE id=:id"), job)
        recovered = await jobs.claim()
        newer = PgProviderBudget(recovered)
        await newer.prepare_request({'model':'kimi-k2.7-code','messages':[]}, provider='moonshot')
        replay = await reserve(newer)
        assert replay['id'] == row['id'] and await newer.claim_dispatch(replay) == response
    else:
        await jobs.cancel(request_job)
        await jobs.finish(job, status='cancelled')
    assert all(a['requests_used']==1 and a['held']==Decimal('2.588672') for a in await accounts(job))
    # Both a retry and a late original settlement may arrive after fencing.
    await asyncio.gather(*(port.settle_recorded(row) for _ in range(4)))
    assert all(a['requests_used']==1 and a['held']==0 and a['settled']==Decimal('0.0000335') for a in await accounts(job))
    assert (await reservation_for(job))['settlement_next_attempt_at'] is None
    if terminal == 'cancelled':
        assert (await jobs.read(request_job))['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_recovery_does_not_invent_missing_usage_or_release_unknown_hold(budget_job):
    from app.services.provider_budgets import recover_next_settlement
    job, port = budget_job
    row = await reserve(port)
    await port.claim_dispatch(row)
    await port.record_receipt(row, provenance=receipt(), response={'actual_reply':'unpriced'})
    for _ in range(10):
        await recover_next_settlement()
        if (await reservation_for(job))['status'] == 'unknown':
            break
    pending = await reservation_for(job)
    assert pending['status'] == 'unknown' and pending['settlement_next_attempt_at'] is None
    assert all(a['held']==Decimal('2.588672') and a['settled']==0 for a in await accounts(job))
    # The dispatcher has no receipt contents or account-mutation privilege.
    from sqlalchemy.exc import DBAPIError
    async with jobs.dispatcher() as conn:
        with pytest.raises(DBAPIError, match='permission denied'):
            await conn.execute(text('SELECT response FROM provider_budget_reservations'))
