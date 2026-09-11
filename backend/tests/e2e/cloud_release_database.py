"""Actual release provenance and delivery immutability/RLS checks in PostgreSQL."""
import asyncio
import sys
from uuid import UUID,uuid4
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.db import tenant_transaction,close_database
from app.domain.identity import user_principal


async def main():
    context=user_principal(sys.argv[1]);release_id,delivery_id=map(UUID,sys.argv[2:4])
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        task=(await conn.execute(text('SELECT * FROM document_engineering_tasks WHERE workflow_run_id=:id'),{'id':release_id})).mappings().one()
        release=(await conn.execute(text('SELECT * FROM document_releases WHERE workflow_run_id=:id'),{'id':release_id})).mappings().one()
        assert task['task_kind']=='release_package' and task['document_id']==release['document_id']
        assert await conn.scalar(text("SELECT count(*) FROM workflow_dispatches WHERE workflow_id=:id AND status='dispatched'"),{'id':release_id})==1
        assert await conn.scalar(text('SELECT count(*) FROM cad_operations WHERE id=:id'),{'id':release_id})==0
        artifacts=(await conn.execute(text('SELECT * FROM artifacts WHERE workflow_run_id=:id'),{'id':release_id})).mappings().all()
        assert len(artifacts)==8 and len({a['attempt_id'] for a in artifacts})==1
        assert all(a['revision_id']==task['source_revision_id'] and str(release_id) in a['filename'] for a in artifacts)
        attempt=(await conn.execute(text('SELECT * FROM execution_attempts WHERE id=:id'),{'id':artifacts[0]['attempt_id']})).mappings().one()
        assert attempt['status']=='succeeded' and len(attempt['execution_payload']['input_artifacts'])==8
        delivery=(await conn.execute(text('SELECT * FROM bridge_deliveries WHERE id=:id'),{'id':delivery_id})).mappings().one()
        assert delivery['release_id']==release_id and delivery['document_id']==task['document_id']
        assert delivery['status']=='delivered' and len(delivery['receipt']['verified_files'])==14
        assert delivery['receipt']['archive_sha256']==delivery['archive_sha256']
        assert delivery['manifest']['source']['revision_id']==str(task['source_revision_id'])
    for sql,key in [("UPDATE document_releases SET release_name='modified' WHERE workflow_run_id=:id",release_id),
                    ("UPDATE bridge_deliveries SET archive_sha256=repeat('a',64) WHERE id=:id",delivery_id),
                    ("UPDATE bridge_deliveries SET receipt='{}'::jsonb WHERE id=:id",delivery_id)]:
        try:
            async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
                await conn.execute(text(sql),{'id':key});await conn.rollback()
        except DBAPIError:pass
        else:raise AssertionError('immutable evidence was mutable')
    outsider=user_principal('release-outsider-'+uuid4().hex)
    async with tenant_transaction(outsider.tenant_id,outsider.principal_id) as conn:
        for table,column,key in [('document_releases','workflow_run_id',release_id),('bridge_deliveries','id',delivery_id),('local_bridges','id',delivery['bridge_id'])]:
            assert await conn.scalar(text(f'SELECT count(*) FROM {table} WHERE {column}=:id'),{'id':key})==0
    print('CAD_RELEASE_DATABASE: native input/output lineage, atomic dispatch, immutable releases and delivery receipts, tenant RLS passed',flush=True)
    await close_database()


asyncio.run(main())
