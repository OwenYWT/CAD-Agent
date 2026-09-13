"""Read-only scene work uses the existing Workflow/Attempt/dispatch authority."""
from __future__ import annotations

import asyncio
import hashlib
from uuid import UUID

from sqlalchemy import text

from app.config import settings
from app.db import tenant_transaction
from app.execution.composition import get_execution_backend
from app.services.cloud_documents import authorized_document, checkpoint
from app.services.document_geometry import _public_scene
from app.services.run_state import create_workflow
from app.services.workflow_dispatch import persist_dispatch
from app.workflows.temporal import _dispatch_after_commit, temporal_check_workflow_id

SCENE_SCHEMA = 'cad-scene.v1'
SCENE_OUTPUTS = {'scene': 'application/json', 'meshes': 'application/zip', 'capability-result': 'application/json'}


async def request_scene(context, document_id: UUID, revision_id: UUID, *, retry_workflow_id: UUID | None = None):
    projection = await checkpoint(context, document_id, revision_id)
    if not projection['fcstd']:
        raise ValueError('此版本没有原生 FCStd，无法生成部件场景')
    # An image inspection resolves development tags; this never starts a kernel.
    runtime = (await asyncio.to_thread(get_execution_backend().runtime_snapshot)).image_digest
    identity = {'doc': document_id, 'revision': revision_id, 'runtime': runtime, 'schema': SCENE_SCHEMA}
    key = hashlib.sha256(f'{context.tenant_id}:{document_id}:{revision_id}:{runtime}:{SCENE_SCHEMA}'.encode()).hexdigest()
    dispatch = None
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        doc = await authorized_document(conn, context, document_id)
        cached = await conn.scalar(text('''SELECT projection FROM document_scenes
            WHERE document_id=:doc AND revision_id=:revision AND runtime_digest=:runtime AND scene_schema=:schema'''), identity)
        if cached is not None:
            return _public_scene(cached, document_id, revision_id)
        # Locks only this disposable cache identity, never the document/head or
        # the modeling write queue. Concurrent authorized viewers share one job.
        await conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': int(key[:16], 16) - 2**63})
        cached = await conn.scalar(text('''SELECT projection FROM document_scenes
            WHERE document_id=:doc AND revision_id=:revision AND runtime_digest=:runtime AND scene_schema=:schema'''), identity)
        if cached is not None:
            return _public_scene(cached, document_id, revision_id)
        previous = (await conn.execute(text('''SELECT t.*, w.status, w.error_code, w.error_message
            FROM document_scene_tasks t JOIN workflow_runs w ON w.id=t.workflow_run_id
            WHERE t.document_id=:doc AND t.revision_id=:revision AND t.runtime_digest=:runtime AND t.scene_schema=:schema
            ORDER BY t.request_number DESC LIMIT 1'''), identity)).mappings().one_or_none()
        retry = previous is not None and (previous['status'] == 'succeeded' or (
            retry_workflow_id == previous['workflow_run_id'] and previous['status'] in {'failed', 'cancelled', 'timed_out'}))
        if previous is None or retry:
            number = previous['request_number'] + 1 if previous else 0
            source = (await conn.execute(text('''SELECT a.*,r.source_workflow_run_id FROM artifacts a
                JOIN project_revisions r ON r.id=a.revision_id AND r.branch_id=:doc
                WHERE a.id=:id AND a.revision_id=:revision AND a.project_id=:project'''),
                {**identity, 'id': UUID(projection['fcstd']['artifact_id']), 'project': doc['project_id']})).mappings().one()
            known = list((await conn.execute(text('''SELECT geometry_sha256 FROM document_geometry_blobs
                WHERE project_id=:project AND runtime_digest=:runtime GROUP BY geometry_sha256
                HAVING count(DISTINCT lod)=3 ORDER BY geometry_sha256 LIMIT 10000'''),
                {'project':doc['project_id'], 'runtime':runtime})).scalars())
            payload = {'document_id': str(document_id), 'source_revision_id': str(revision_id),
                'source_artifact_id': str(source['id']), 'source_sha256': source['sha256'],
                'source_workflow_run_id': str(source['source_workflow_run_id']), 'timeout_seconds':120,
                'scene_task': {'runtime_digest':runtime, 'schema_version':SCENE_SCHEMA, 'known_definitions':known}}
            created = await create_workflow(conn, tenant_id=context.tenant_id, project_id=doc['project_id'],
                requested_by_principal_id=context.principal_id, kind='mcad.scene',
                idempotency_key=f'scene:{key}:{number}', request_payload=payload)
            workflow_id = created.workflow_id
            await conn.execute(text('''INSERT INTO document_scene_tasks(workflow_run_id,tenant_id,project_id,document_id,
                revision_id,source_artifact_id,source_sha256,principal_id,runtime_digest,scene_schema,request_number)
                VALUES(:workflow,:tenant,:project,:doc,:revision,:artifact,:sha,:principal,:runtime,:schema,:number)'''),
                {**identity, 'workflow':workflow_id, 'tenant':context.tenant_id, 'project':doc['project_id'],
                 'artifact':source['id'], 'sha':source['sha256'], 'principal':context.principal_id, 'number':number})
            dispatch = await persist_dispatch(conn, tenant_id=context.tenant_id, principal_id=context.principal_id,
                workflow_id=workflow_id, workflow_type='McadCheckWorkflow', temporal_id=temporal_check_workflow_id(workflow_id),
                task_queue=settings.temporal_task_queue, payload={**payload, 'workflow_run_id':str(workflow_id),
                    'tenant_id':str(context.tenant_id), 'project_id':str(doc['project_id']), 'principal_id':str(context.principal_id)})
            job = {'workflow_run_id':str(workflow_id), 'status':'pending', 'error_code':None, 'error_message':None}
        else:
            job = {key: str(previous[key]) if key == 'workflow_run_id' else previous[key]
                   for key in ('workflow_run_id', 'status', 'error_code', 'error_message')}
    if dispatch is not None:
        await _dispatch_after_commit(dispatch)
    return {**job, 'document_id':str(document_id), 'revision_id':str(revision_id), 'scene_schema':SCENE_SCHEMA,
            'scene_url':f'/api/documents/{document_id}/scenes/{revision_id}', 'retry_after_ms':1000}


async def scene_evidence_access(conn, attempt, revision_id, principal_id, kinds):
    from types import SimpleNamespace
    if not set(kinds) <= set(SCENE_OUTPUTS):
        raise ValueError('undeclared scene artifact kind')
    row = (await conn.execute(text('''SELECT t.*,r.source_workflow_run_id FROM document_scene_tasks t
        JOIN project_revisions r ON r.id=t.revision_id AND r.branch_id=t.document_id
        JOIN artifacts a ON a.id=t.source_artifact_id AND a.revision_id=t.revision_id AND a.sha256=t.source_sha256
        WHERE t.workflow_run_id=:workflow AND t.revision_id=:revision AND t.principal_id=:principal'''),
        {'workflow':attempt['workflow_run_id'], 'revision':revision_id, 'principal':principal_id})).mappings().one_or_none()
    request = dict(attempt['request_payload'])
    if row is None or any(request.get(k) != str(row[column]) for k,column in (
        ('document_id','document_id'), ('source_revision_id','revision_id'), ('source_artifact_id','source_artifact_id'),
        ('source_sha256','source_sha256'), ('source_workflow_run_id','source_workflow_run_id'))):
        raise ValueError('scene source does not match its immutable task')
    await authorized_document(conn, SimpleNamespace(tenant_id=attempt['tenant_id'],principal_id=principal_id), row['document_id'])
