"""Scene derivations share fenced execution, artifact promotion and cancellation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import text
from temporalio.exceptions import ApplicationError

from app.db import tenant_transaction
from app.domain.runs import WorkflowStatus
from app.execution.contracts import ArtifactInput
from app.object_store import download_object
from app.repositories.runs import append_workflow_event
from app.services.cloud_documents import authorized_document
from app.services.document_geometry import build_document_scene, MAX_BYTES
from app.services.run_state import transition_workflow
from app.services.scene_jobs import SCENE_OUTPUTS


def _context(payload):
    return SimpleNamespace(tenant_id=UUID(payload['tenant_id']), principal_id=UUID(payload['principal_id']))


async def materialize_scene_input(payload, directory):
    context = _context(payload)
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        row = (await conn.execute(text('''SELECT a.*,t.document_id,t.runtime_digest,t.scene_schema FROM document_scene_tasks t
            JOIN artifacts a ON a.id=t.source_artifact_id AND a.revision_id=t.revision_id
                AND a.project_id=t.project_id AND a.sha256=t.source_sha256
            WHERE t.workflow_run_id=:workflow AND t.principal_id=:principal
                AND t.project_id=:project AND t.revision_id=:revision'''),
            {'workflow':UUID(payload['workflow_run_id']), 'principal':context.principal_id,
             'project':UUID(payload['project_id']), 'revision':UUID(payload['revision_id'])})).mappings().one_or_none()
        if row is None or payload.get('input_artifacts') != [{'artifact_id':str(row['id']), 'filename':'checkpoint.FCStd'}] or (
            row['runtime_digest'] != payload['scene_task']['runtime_digest'] or row['scene_schema'] != payload['scene_task']['schema_version']):
            raise ApplicationError('场景输入与持久任务来源不一致', type='scene_source_mismatch', non_retryable=True)
        await authorized_document(conn, context, row['document_id'])
    path = Path(directory) / 'checkpoint.FCStd'
    measured = await download_object(row['object_key'], path)
    if measured['sha256'] != row['sha256'] or measured['size_bytes'] != row['size_bytes']:
        raise ApplicationError('场景输入完整性校验失败', type='scene_source_integrity', non_retryable=True)
    return (ArtifactInput(artifact_id=str(row['id']), filename=path.name, sha256=row['sha256'],
                         size_bytes=row['size_bytes'], media_type=row['content_type']),), {str(row['id']):path}


async def compute_scene(activities, payload):
    context = _context(payload)
    workflow_id, document_id, revision_id = (UUID(payload[key]) for key in ('workflow_run_id','document_id','source_revision_id'))
    try:
        async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
            row = (await conn.execute(text('''SELECT w.request_payload,w.status FROM workflow_runs w
                JOIN document_scene_tasks t ON t.workflow_run_id=w.id
                WHERE w.id=:id AND w.kind='mcad.scene' AND t.principal_id=:principal FOR UPDATE OF w'''),
                {'id':workflow_id,'principal':context.principal_id})).mappings().one()
            if any(payload.get(key) != value for key,value in row['request_payload'].items()):
                raise ValueError('场景执行请求与已保存的任务不一致')
            await authorized_document(conn, context, document_id)
            status = WorkflowStatus(row['status'])
            for before, after in ((WorkflowStatus.PENDING,WorkflowStatus.PLANNING),(WorkflowStatus.PLANNING,WorkflowStatus.RUNNING)):
                if status == before:
                    await transition_workflow(conn,workflow_id,expected=before,target=after); status=after
            if status not in {WorkflowStatus.RUNNING,WorkflowStatus.SUCCEEDED}:
                raise ApplicationError('场景计算已取消或终止',type='scene_terminal',non_retryable=True)
        runtime = await asyncio.to_thread(activities.backend.runtime_snapshot)
        if runtime.image_digest != payload['scene_task']['runtime_digest']:
            raise ValueError('场景任务对应的内核版本已不可用，请按当前运行时重新请求')

        async def execute_scene(source, known, directory):
            if str(source['id']) != payload['source_artifact_id'] or source['sha256'] != payload['source_sha256']:
                raise ValueError('场景检查点与冻结来源不一致')
            result = await activities.execute({**payload, 'revision_id':str(revision_id), 'expected_base_revision_id':str(revision_id),
                'step_index':0, 'input_artifacts':[{'artifact_id':payload['source_artifact_id'],'filename':'checkpoint.FCStd'}],
                'execution':{'step_key':'scene_compute','kind':'scene_compute','capability':'mcad.freecad','operation':'scene',
                    'mode':'analysis','source_language':'json','timeout_seconds':120,
                    'source_code':json.dumps({'schema_version':'mcad-capability-task.v1','capability':'freecad','operation':'scene',
                        'params':{'known_definitions':known}, 'inputs':{'base':'checkpoint.FCStd'}},sort_keys=True),
                    'outputs':[{'name':kind,'media_type':media,'max_size_bytes':MAX_BYTES} for kind,media in SCENE_OUTPUTS.items()]}})
            if {a['artifact_kind'] for a in result['artifacts']} != set(SCENE_OUTPUTS):
                raise ValueError('场景执行缺少声明产物')
            files = {}
            for artifact in result['artifacts']:
                path = directory / artifact['artifact_kind']
                measured = await download_object(artifact['object_key'],path)
                if measured['sha256'] != artifact['sha256'] or measured['size_bytes'] != artifact['size_bytes']:
                    raise ValueError('持久场景输出完整性校验失败')
                files[artifact['artifact_kind']] = path
            return files

        async def complete_job(conn):
            await authorized_document(conn,context,document_id)
            status = await conn.scalar(text('SELECT status FROM workflow_runs WHERE id=:id FOR UPDATE'),{'id':workflow_id})
            if status == 'running':
                await transition_workflow(conn,workflow_id,expected=WorkflowStatus.RUNNING,target=WorkflowStatus.SUCCEEDED)
                await append_workflow_event(conn,tenant_id=context.tenant_id,workflow_id=workflow_id,event_type='scene.completed',
                    payload={'source_revision_id':str(revision_id),'source_sha256':payload['source_sha256'],
                             'runtime_digest':runtime.image_digest,'scene_schema':payload['scene_task']['schema_version']})
            elif status != 'succeeded':
                raise ApplicationError('场景结果发布前任务已取消或终止',type='scene_terminal',non_retryable=True)

        await build_document_scene(context,document_id,revision_id,payload['scene_task'],execute_scene,complete_job)
        return {'status':'succeeded','source_revision_id':str(revision_id),'scene_schema':payload['scene_task']['schema_version']}
    except (KeyError,ValueError,PermissionError) as exc:
        raise ApplicationError(str(exc),type='scene_request_invalid',non_retryable=True) from exc
