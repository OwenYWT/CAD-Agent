"""Read actual branch evidence; prohibited writes and a queued rollback stay atomic.

Run in the acceptance API container with raw auth subject and branch IDs.
Every attempted write is rejected or explicitly rolled back.
"""
import asyncio
import sys
from uuid import UUID,uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import close_database,tenant_transaction
from app.domain.identity import user_principal
from app.services.cloud_documents import authorized_document,checkpoint
from app.services.document_branches import _queue
from app.workflows.temporal import FreeCADRevisionRestoreV1


async def main():
    owner=user_principal(sys.argv[1]);document_id=UUID(sys.argv[2])
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        doc=await authorized_document(conn,owner,document_id)
        fork=(await conn.execute(text('SELECT * FROM document_forks WHERE document_id=:id'),{'id':document_id})).mappings().one()
        assert fork['workflow_run_id'] is not None
        for table,column in [('workflow_runs','id'),('cad_operations','id'),('workflow_dispatches','workflow_id')]:
            assert await conn.scalar(text(f'SELECT count(*) FROM {table} WHERE {column}=:id'),{'id':fork['workflow_run_id']})==1
    for sql in ('UPDATE document_forks SET request_hash=repeat(\'a\',64) WHERE document_id=:id',
                'UPDATE document_forks SET workflow_run_id=:workflow WHERE document_id=:id'):
        try:
            async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
                await conn.execute(text(sql),{'id':document_id,'workflow':uuid4()})
                await conn.rollback()
        except DBAPIError:
            pass
        else:
            raise AssertionError('immutable branch evidence accepted a mutation')
    outside=user_principal('outside-branch-acceptance-'+uuid4().hex)
    async with tenant_transaction(outside.tenant_id,outside.principal_id) as conn:
        assert await conn.scalar(text('SELECT count(*) FROM document_forks WHERE document_id=:id'),{'id':document_id})==0
    source=await checkpoint(owner,document_id,doc['head_revision_id'])
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        workflow_id,_=await _queue(conn,owner,doc,key='rollback-branch-'+uuid4().hex,objective='Rollback-only atomic evidence check',
            rule='explicit_branch_fork',source=source['fcstd'],restore=FreeCADRevisionRestoreV1(
                source_revision_id=doc['head_revision_id'],source_artifact_id=UUID(source['fcstd']['artifact_id']),source_sha256=source['fcstd']['sha256']))
        await conn.rollback()
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        for table,column in [('workflow_runs','id'),('cad_operations','id'),('workflow_dispatches','workflow_id')]:
            assert await conn.scalar(text(f'SELECT count(*) FROM {table} WHERE {column}=:id'),{'id':workflow_id})==0
    print('actual branch linkage, tenant isolation, immutability and workflow/queue/dispatch rollback passed')
    await close_database()


asyncio.run(main())
