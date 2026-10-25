"""Real previous-schema upgrade with pending work; never downgrade a shared DB."""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4, uuid5

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

URL = os.getenv('CAD_MIGRATION_TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not URL, reason='requires dedicated empty migration database')


def test_upgrade_preserves_pending_job_and_unfinished_usage(monkeypatch):
    from app.config import settings
    from app.db import close_database, get_database_engine, tenant_transaction
    from app.domain.identity import user_principal
    from app.repositories.identity import reconcile_principal
    from app.repositories.projects import create_project
    from app.services.run_state import create_workflow
    from app.services.llm_usage import UsageContext, usage_context, start_call
    from app.services import model_jobs
    from app.execution.canonical import canonical_sha256

    monkeypatch.setattr(settings, 'database_url', URL)
    monkeypatch.setattr(settings, 'durable_control_plane_enabled', True)
    monkeypatch.setenv('DATABASE_MIGRATION_URL', URL)
    config = Config(str(Path(__file__).resolve().parents[2] / 'alembic.ini'))
    config.set_main_option('sqlalchemy.url', URL.replace('%', '%%'))
    command.upgrade(config, '0028_llm_monitoring')
    owner = user_principal(f'upgrade-{uuid4()}')
    project = uuid4()

    async def old_run():
        await reconcile_principal(owner)
        async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
            await create_project(conn, project_id=project, tenant_id=owner.tenant_id,
                creator_principal_id=owner.principal_id, name='upgrade contract', slug=str(project))
            run = await create_workflow(conn, tenant_id=owner.tenant_id, project_id=project,
                requested_by_principal_id=owner.principal_id, kind='mcad.agent.v2.generate',
                idempotency_key=str(uuid4()), request_payload={})
        token = usage_context.set(UsageContext(owner.tenant_id, owner.principal_id))
        try:
            assert await start_call(provider='upgrade-fixture', model='contract', request_hash='a'*64, attempt=1)
        finally:
            usage_context.reset(token)
            await close_database()
        return run.workflow_id

    run = asyncio.run(old_run())
    command.upgrade(config, '0029_model_jobs')
    request = {'job_id': str(uuid4()), 'operation': 'agent_v2.requirements', 'payload': {
        'tenant_id': str(owner.tenant_id), 'principal_id': str(owner.principal_id),
        'workflow_run_id': str(run), 'dimension': 6.5}}

    async def old_job():
        # Persist the 0029 row shape before installing newer schema. The
        # current submit path correctly requires the current ledger tables.
        async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
            await conn.execute(text('''INSERT INTO model_jobs
                (id,tenant_id,principal_id,workflow_run_id,operation,payload,payload_hash)
                VALUES(:id,:tenant,:principal,:run,:operation,CAST(:payload AS jsonb),:digest)'''),
                dict(id=request['job_id'],tenant=owner.tenant_id,principal=owner.principal_id,
                    run=run,operation=request['operation'],payload=json.dumps(request['payload']),
                    digest=canonical_sha256({'operation':request['operation'],'payload':request['payload']})))
        await close_database()
    asyncio.run(old_job())
    command.upgrade(config, 'head')

    async def verify():
        async with get_database_engine().connect() as conn:
            assert await conn.scalar(text('SELECT count(*) FROM provider_budget_policies')) == 0
        assert (await model_jobs.submit(request))['status'] == 'queued'
        async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
            row = (await conn.execute(text('SELECT payload,generation,status FROM model_jobs WHERE id=:id'),
                                      {'id': request['job_id']})).mappings().one()
            assert row['payload'] == request['payload'] and row['generation'] == 0
            assert row['status'] == 'queued'
            call = (await conn.execute(text('SELECT status,total_tokens FROM llm_calls WHERE principal_id=:id'),
                                       {'id': owner.principal_id})).mappings().one()
            assert call['status'] == 'started' and call['total_tokens'] is None
            assert await conn.scalar(text("SELECT data_type FROM information_schema.columns WHERE table_name='model_jobs' AND column_name='payload'")) == 'json'
            policy=await conn.scalar(text('SELECT policy FROM provider_budget_policies WHERE workflow_run_id=:id'),{'id':run})
            assert policy['mode']=='requests' and policy['legacy_admission'] is True
        async with get_database_engine().connect() as conn:
            assert await conn.scalar(text('SELECT version_num FROM alembic_version')) == '0033_tenant_billing_foundation'
        async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
            from app.services.customer_billing import account_summary
            summary = await account_summary(conn, owner.tenant_id, owner.principal_id)
            assert summary['plan']['code'] == 'basic'
            assert summary['plan']['monthly_provider_requests'] is None
            assert summary['wallet']['balance_minor'] == 0
        await close_database()
    asyncio.run(verify())


def test_receipt_queue_upgrade_preserves_legacy_holds_and_blocks_lossy_downgrade(monkeypatch):
    """An isolated 0031 ledger is upgraded with an actual unresolved dispatch."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.config import settings
    from app.db import close_database, tenant_transaction
    from app.domain.identity import user_principal
    from app.repositories.identity import reconcile_principal
    from app.repositories.projects import create_project
    from app.services.run_state import create_workflow
    from app.services import model_jobs
    from app.services.provider_budgets import PgProviderBudget, admit, freeze_policy, recover_next_settlement
    from tests.test_provider_budget_policy import cost_contract
    from decimal import Decimal

    name = 'cad_receipt_upgrade_' + uuid4().hex
    url = URL.rsplit('/', 1)[0] + '/' + name
    async def database(create):
        engine = create_async_engine(URL, isolation_level='AUTOCOMMIT')
        try:
            async with engine.connect() as conn:
                await conn.execute(text(f'CREATE DATABASE {name}' if create else f'DROP DATABASE {name}'))
        finally:
            await engine.dispose()
    asyncio.run(database(True))
    monkeypatch.setattr(settings, 'database_url', url)
    monkeypatch.setattr(settings, 'durable_control_plane_enabled', True)
    monkeypatch.setenv('DATABASE_MIGRATION_URL', url)
    config = Config(str(Path(__file__).resolve().parents[2] / 'alembic.ini'))
    config.set_main_option('sqlalchemy.url', url.replace('%', '%%'))
    try:
        command.upgrade(config, '0031_provider_budgets')
        async def seed():
            await close_database()
            owner = user_principal(f'receipt-upgrade-{uuid4()}')
            await reconcile_principal(owner)
            project = uuid4()
            async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
                await create_project(conn, project_id=project, tenant_id=owner.tenant_id,
                    creator_principal_id=owner.principal_id, name='receipt upgrade', slug=str(project))
                run = await create_workflow(conn, tenant_id=owner.tenant_id, project_id=project,
                    requested_by_principal_id=owner.principal_id, kind='mcad.agent.v2.generate',
                    idempotency_key=str(uuid4()), request_payload={})
                await admit(conn, tenant_id=owner.tenant_id, workflow_id=run.workflow_id,
                    policy=freeze_policy(mode='hard_cost', request_limit=8, month_request_limit=20, cost_contract=cost_contract()))
            request = {'job_id':str(uuid4()), 'operation':'agent_v2.requirements', 'payload':{
                'tenant_id':str(owner.tenant_id), 'principal_id':str(owner.principal_id), 'workflow_run_id':str(run.workflow_id)}}
            await model_jobs.submit(request)
            job = await model_jobs.claim()
            port = PgProviderBudget(job)
            await port.prepare_request({'model':'kimi-k2.7-code', 'messages':[]}, provider='moonshot')
            # Materialize the historical 0031 row contract directly. The current
            # dispatcher requires 0033 customer admission and cannot run against
            # an old schema; no provider call is involved in this migration test.
            async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
                policy = port.policy
                amount = Decimal(policy['reservation'])
                identity = uuid5(job['id'], f"{job['generation']}:legacy:1")
                row = dict((await conn.execute(text('''INSERT INTO provider_budget_reservations
                    (id,tenant_id,workflow_run_id,model_job_id,generation,logical_call_id,attempt,
                     request_digest,provider,model,policy,reserved_amount,status)
                    VALUES(:id,:tenant,:run,:job,:generation,'legacy',1,'legacy-digest','moonshot',
                           'kimi-k2.7-code',CAST(:policy AS jsonb),:amount,'reserved') RETURNING *'''),
                    dict(id=identity,tenant=owner.tenant_id,run=run.workflow_id,job=job['id'],
                         generation=job['generation'],policy=json.dumps(policy),amount=amount))).mappings().one())
                month = await conn.scalar(text("SELECT to_char(now() AT TIME ZONE 'UTC','YYYY-MM')"))
                for scope, limit, money in [
                    (f'task:{run.workflow_id}',policy['request_limit'],policy['cost']['task_limit']),
                    (f'month:{month}',policy['month_request_limit'],policy['cost']['month_limit']),
                ]:
                    account = uuid5(owner.tenant_id, scope)
                    await conn.execute(text('''INSERT INTO provider_budget_accounts
                        (id,tenant_id,scope,request_limit,currency,cost_limit,policy,requests_used,held)
                        VALUES(:id,:tenant,:scope,:limit,'CNY',:money,CAST(:policy AS jsonb),1,:amount)'''),
                        dict(id=account,tenant=owner.tenant_id,scope=scope,limit=limit,
                             money=Decimal(money),policy=json.dumps({'mode':policy['mode']}),amount=amount))
                    await conn.execute(text('''INSERT INTO provider_budget_allocations
                        (reservation_id,account_id,tenant_id) VALUES(:id,:account,:tenant)'''),
                        dict(id=identity,account=account,tenant=owner.tenant_id))
            await port.claim_dispatch(row)
            await model_jobs.finish(job, status='succeeded', result={'legacy_response':True})
            await close_database()
            return job, row
        job, row = asyncio.run(seed())
        command.upgrade(config, 'head')
        async def record_late_receipt():
            async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
                pending = (await conn.execute(text('SELECT * FROM provider_budget_reservations WHERE id=:id'), row)).mappings().one()
                assert pending['status']=='dispatch_claimed' and pending['response'] is None
                assert pending['settlement_attempts']==0 and pending['settlement_next_attempt_at'] is None
                accounts = (await conn.execute(text('SELECT held,settled FROM provider_budget_accounts'))).mappings().all()
                assert len(accounts)==2 and all(a['held']==Decimal('2.588672') and a['settled']==0 for a in accounts)
                from app.services.customer_billing import account_summary
                summary = await account_summary(conn, job['tenant_id'], job['principal_id'])
                assert summary['plan']['monthly_provider_requests'] is None
                assert summary['usage']['provider_requests'] == 1
                assert summary['wallet']['balance_minor'] == 0
            assert not await recover_next_settlement()  # An absent receipt cannot be invented by migration.
            await PgProviderBudget(job).record_receipt(row, provenance={'provider':'moonshot','request_model':'kimi-k2.7-code',
                'model':'kimi-k2.7-code','request_hash':'legacy-digest','usage':{'prompt_tokens':1,'completion_tokens':1}},
                response={'legacy_response':True})
            await close_database()
        asyncio.run(record_late_receipt())
        with pytest.raises(Exception, match='Drain pending provider settlements'):
            command.downgrade(config, '0031_provider_budgets')
        async def recover():
            assert await recover_next_settlement()
            async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
                settled = (await conn.execute(text('SELECT * FROM provider_budget_reservations WHERE id=:id'), row)).mappings().one()
                assert settled['status']=='settled' and settled['settlement_next_attempt_at'] is None
                assert settled['response']=={'legacy_response':True}
                assert await conn.scalar(text('SELECT status FROM model_jobs WHERE id=:id'), job)=='succeeded'
                accounts = (await conn.execute(text('SELECT held,settled FROM provider_budget_accounts'))).mappings().all()
                assert all(a['held']==0 and a['settled']==Decimal('0.0000335') for a in accounts)
            await close_database()
        asyncio.run(recover())
        command.downgrade(config, '0031_provider_budgets')
        command.upgrade(config, 'head')
        async def credit():
            from app.services.customer_billing import adjust_wallet
            async with tenant_transaction(job['tenant_id'], role='billing_admin') as conn:
                await adjust_wallet(conn, tenant_id=job['tenant_id'], actor_subject='migration-test',
                    idempotency_key='preserve-credit', amount_minor=500, reference='manual-test', reason='upgrade test')
            await close_database()
        asyncio.run(credit())
        with pytest.raises(Exception, match='Customer billing records must be preserved'):
            command.downgrade(config, '0032_provider_receipt_recovery')
        async def preserved():
            from app.db import get_database_engine
            async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
                assert await conn.scalar(text('SELECT balance_minor FROM tenant_wallet_accounts')) == 500
                assert await conn.scalar(text('SELECT sum(amount_minor) FROM tenant_wallet_entries')) == 500
            # Even the migration owner cannot silently rewrite audit history.
            for table in ('billing_plan_versions', 'tenant_wallet_entries'):
                with pytest.raises(Exception, match='billing history is append-only'):
                    async with get_database_engine().begin() as conn:
                        await conn.execute(text(f'DELETE FROM {table}'))
            await close_database()
        asyncio.run(preserved())
    finally:
        asyncio.run(close_database())
        asyncio.run(database(False))
