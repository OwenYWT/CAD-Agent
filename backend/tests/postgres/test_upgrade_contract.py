"""Real previous-schema upgrade with pending work; never downgrade a shared DB."""
import asyncio
import os
from pathlib import Path
from uuid import uuid4

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
        result = await model_jobs.submit(request)
        assert result['status'] == 'queued'
        await close_database()
    asyncio.run(old_job())
    command.upgrade(config, 'head')

    async def verify():
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
        async with get_database_engine().connect() as conn:
            assert await conn.scalar(text('SELECT version_num FROM alembic_version')) == '0030_model_job_json_numbers'
        await close_database()
    asyncio.run(verify())
