"""Durable, revision-bound engineering computations using the native checkpoint."""
from __future__ import annotations

import hashlib
import json
from uuid import UUID

from sqlalchemy import text

from app.config import settings
from app.db import tenant_transaction
from app.domain.projects import Permission
from app.execution.canonical import canonical_sha256
from app.freecad.engineering_contracts import engineering_outputs, EngineeringSubmission
from app.object_store import get_object
from app.services.cloud_documents import DocumentConflict, authorized_document, checkpoint
from app.services.run_state import IdempotencyConflict, create_workflow
from app.services.workflow_dispatch import persist_dispatch
from app.workflows.temporal import _dispatch_after_commit, temporal_check_workflow_id


async def queue_engineering(conn, context, doc, source, task, *, idempotency_key, request_hash):
    """Called inside the caller's authorized document transaction."""
    payload = {'document_id': str(doc['id']), 'source_revision_id': str(doc['head_revision_id']),
        'source_state_version': doc['state_version'], 'source_workflow_run_id': str(source['source_workflow_run_id']),
        'source_artifact_id': str(source['id']), 'source_sha256': source['sha256'], 'engineering_task': task, 'timeout_seconds': 300}
    created = await create_workflow(conn, tenant_id=context.tenant_id, project_id=doc['project_id'],
        requested_by_principal_id=context.principal_id, kind='mcad.engineering',
        idempotency_key=f'engineering:{context.principal_id}:{idempotency_key}', request_payload=payload)
    workflow_id = created.workflow_id
    await conn.execute(text('''INSERT INTO document_engineering_tasks(workflow_run_id,tenant_id,project_id,document_id,
        source_revision_id,source_state_version,source_artifact_id,source_sha256,task_kind,principal_id,idempotency_key,request_hash)
        VALUES(:workflow,:tenant,:project,:doc,:revision,:version,:artifact,:sha,:kind,:principal,:key,:hash)'''),
        {'workflow': workflow_id, 'tenant': context.tenant_id, 'project': doc['project_id'], 'doc': doc['id'],
         'revision': doc['head_revision_id'], 'version': doc['state_version'], 'artifact': source['id'],
         'sha': source['sha256'], 'kind': task['kind'], 'principal': context.principal_id, 'key': idempotency_key, 'hash': request_hash})
    dispatch = await persist_dispatch(conn, tenant_id=context.tenant_id, principal_id=context.principal_id,
        workflow_id=workflow_id, workflow_type='McadCheckWorkflow', temporal_id=temporal_check_workflow_id(workflow_id),
        task_queue=settings.temporal_task_queue, payload={**payload, 'workflow_run_id': str(workflow_id),
            'tenant_id': str(context.tenant_id), 'project_id': str(doc['project_id']), 'principal_id': str(context.principal_id)})
    return workflow_id, dispatch


async def submit_engineering(context, document_id: UUID, submission: EngineeringSubmission):
    digest = canonical_sha256({'document_id': str(document_id), **submission.model_dump(mode='json', exclude={'idempotency_key'})})
    projection = await checkpoint(context, document_id, submission.expected_revision_id)
    if projection['fcstd'] is None:
        raise ValueError('工程计算需要已提交的原生 FCStd 检查点')
    if submission.task.kind == 'native_measure':
        names={feature['kernel_name'] for feature in projection['features']}
        if submission.task.component_name not in names or submission.task.other_component_name and submission.task.other_component_name not in names:
            raise ValueError('测量对象不属于此版本')
        from app.topology.contracts import FreeCADTopologySelector
        bindings = [FreeCADTopologySelector.model_validate(binding) for feature in projection['features'] for binding in feature.get('topology_bindings', [])]
        for selector in submission.task.selectors:
            parsed = FreeCADTopologySelector.model_validate(selector)
            if parsed.revision_id != submission.expected_revision_id or parsed not in bindings:
                raise ValueError('测量选择缺少当前版本的完整且唯一的拓扑依据')
    dispatch = None
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        doc = await authorized_document(conn, context, document_id, Permission.RUN_VALIDATION)
        await conn.execute(text('SELECT id FROM projects WHERE id=:id FOR UPDATE'), {'id': doc['project_id']})
        previous = (await conn.execute(text('SELECT * FROM document_engineering_tasks WHERE principal_id=:principal AND idempotency_key=:key'),
            {'principal': context.principal_id, 'key': submission.idempotency_key})).mappings().one_or_none()
        if previous is not None:
            if previous['request_hash'] != digest:
                raise IdempotencyConflict('工程任务请求 ID 已用于其他内容')
            workflow_id = previous['workflow_run_id']
        else:
            doc = await authorized_document(conn, context, document_id, Permission.RUN_VALIDATION, lock=True)
            if doc['head_revision_id'] != submission.expected_revision_id or doc['state_version'] != submission.expected_state_version:
                raise DocumentConflict('模型已更新，请刷新后重新设置工程计算')
            source = (await conn.execute(text('SELECT a.*,r.source_workflow_run_id FROM artifacts a JOIN project_revisions r ON r.id=a.revision_id WHERE a.id=:id AND a.revision_id=:revision AND r.branch_id=:doc'),
                {'id': UUID(projection['fcstd']['artifact_id']), 'revision': submission.expected_revision_id, 'doc': document_id})).mappings().one()
            workflow_id, dispatch = await queue_engineering(conn, context, doc, source, submission.task.model_dump(mode='json'),
                idempotency_key=submission.idempotency_key, request_hash=digest)
    if dispatch is not None:
        await _dispatch_after_commit(dispatch)
    return {'workflow_run_id': str(workflow_id), 'tenant_id': str(context.tenant_id), 'replayed': previous is not None}


async def engineering_tasks(context, document_id):
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        await authorized_document(conn, context, document_id)
        rows = (await conn.execute(text('''SELECT t.workflow_run_id,t.source_revision_id,t.source_state_version,t.task_kind,t.created_at,
            w.status,w.error_code,w.error_message,w.request_payload->'engineering_task' AS task
            FROM document_engineering_tasks t JOIN workflow_runs w ON w.id=t.workflow_run_id
            WHERE t.document_id=:doc AND t.task_kind IN ('linear_static','contour_milling','native_measure')
            ORDER BY t.created_at DESC,t.workflow_run_id DESC LIMIT 50'''), {'doc': document_id})).mappings().all()
        return {'tasks': [dict(row) for row in rows]}


async def engineering_result(context, document_id, workflow_id):
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        await authorized_document(conn, context, document_id)
        task = (await conn.execute(text('''SELECT t.*,w.status FROM document_engineering_tasks t
            JOIN workflow_runs w ON w.id=t.workflow_run_id WHERE t.document_id=:doc AND t.workflow_run_id=:workflow'''),
            {'doc': document_id, 'workflow': workflow_id})).mappings().one_or_none()
        if task is None:
            raise KeyError(workflow_id)
        if task['status'] != 'succeeded':
            raise DocumentConflict('工程计算尚未成功完成')
        outputs=engineering_outputs(task['task_kind'])
        artifacts = [dict(row) for row in (await conn.execute(text('SELECT * FROM artifacts WHERE workflow_run_id=:workflow AND revision_id=:revision'),
            {'workflow': workflow_id, 'revision': task['source_revision_id']})).mappings() if row['artifact_kind'] in outputs]
    if {a['artifact_kind'] for a in artifacts} != set(outputs):
        raise ValueError('工程结果缺少完整工件')
    report_row = next(a for a in artifacts if a['artifact_kind'] == 'engineering_report')
    raw = await get_object(report_row['object_key'])
    if hashlib.sha256(raw).hexdigest() != report_row['sha256'] or len(raw) != report_row['size_bytes']:
        raise ValueError('工程报告完整性验证失败')
    report = json.loads(raw)
    if report.get('source_fcstd_sha256') != task['source_sha256'] or report.get('kind') != task['task_kind']:
        raise ValueError('工程报告与来源检查点不一致')
    return {'workflow_run_id': str(workflow_id), 'source_revision_id': str(task['source_revision_id']),
        'source_state_version': task['source_state_version'], 'report': report,
        'artifacts': {a['artifact_kind']: {'artifact_id': str(a['id']), 'filename': a['filename'], 'sha256': a['sha256'],
            'size_bytes': a['size_bytes'], 'url': f"/api/documents/{document_id}/artifacts/{a['id']}"} for a in artifacts}}
