"""Verify real task/attempt/artifact provenance, immutability and tenant isolation."""
import asyncio
import sys
from uuid import UUID,uuid4
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.db import tenant_transaction,close_database
from app.domain.identity import user_principal


async def main():
    owner=user_principal(sys.argv[1]);workflow_id=UUID(sys.argv[2])
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        task=(await conn.execute(text('SELECT * FROM document_engineering_tasks WHERE workflow_run_id=:id'),{'id':workflow_id})).mappings().one()
        assert await conn.scalar(text("SELECT count(*) FROM workflow_dispatches WHERE workflow_id=:id AND status='dispatched'"),{'id':workflow_id})==1
        assert await conn.scalar(text('SELECT count(*) FROM cad_operations WHERE id=:id'),{'id':workflow_id})==0
        rows=(await conn.execute(text('SELECT * FROM artifacts WHERE workflow_run_id=:id'),{'id':workflow_id})).mappings().all()
        assert len(rows)==(5 if task['task_kind']=='contour_milling' else 4) and len({r['attempt_id'] for r in rows})==1
        assert all(r['revision_id']==task['source_revision_id'] and str(workflow_id) in r['filename'] for r in rows)
        attempt=(await conn.execute(text('SELECT * FROM execution_attempts WHERE id=:id'),{'id':rows[0]['attempt_id']})).mappings().one()
        assert attempt['status']=='succeeded'
        assert attempt['execution_payload']['input_artifacts'][0]['artifact_id']==str(task['source_artifact_id'])
        assert all(r['runtime_metadata']['image_digest'].startswith('sha256:') for r in rows)
    try:
        async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
            await conn.execute(text("UPDATE document_engineering_tasks SET source_sha256=repeat('a',64) WHERE workflow_run_id=:id"),{'id':workflow_id})
            await conn.rollback()
    except DBAPIError:
        pass
    else:
        raise AssertionError('immutable source accepted mutation')
    outside=user_principal('engineering-outsider-'+uuid4().hex)
    async with tenant_transaction(outside.tenant_id,outside.principal_id) as conn:
        assert await conn.scalar(text('SELECT count(*) FROM document_engineering_tasks WHERE workflow_run_id=:id'),{'id':workflow_id})==0
    print('real dispatch, input/output lineage, sealed runtime, no CAD mutation, immutable provenance and RLS passed',flush=True)
    await close_database()


asyncio.run(main())
