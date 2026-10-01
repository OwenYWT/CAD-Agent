"""Native engineering activity, sharing the normal execution/lease/artifact path."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import text
from temporalio.exceptions import ApplicationError

from app.db import tenant_transaction
from app.domain.runs import WorkflowStatus
from app.execution.contracts import ArtifactInput
from app.freecad.engineering_contracts import ENGINEERING_TASK, engineering_outputs, engineering_permissions
from app.freecad.release_contracts import ReleaseTask
from app.object_store import download_object, get_object
from app.repositories.runs import append_workflow_event
from app.services.cloud_documents import authorized_document
from app.services.run_state import transition_workflow


async def materialize_engineering_input(payload, directory):
    """Accept only the exact frozen FCStd owned by this engineering task."""
    context = SimpleNamespace(tenant_id=UUID(payload['tenant_id']), principal_id=UUID(payload['principal_id']))
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        row = (await conn.execute(text('''SELECT a.*,t.document_id,t.task_kind,w.request_payload FROM document_engineering_tasks t
            JOIN workflow_runs w ON w.id=t.workflow_run_id
            JOIN artifacts a ON a.id=t.source_artifact_id AND a.project_id=t.project_id AND a.revision_id=t.source_revision_id
                AND a.sha256=t.source_sha256
            WHERE t.workflow_run_id=:workflow AND t.principal_id=:principal AND t.project_id=:project AND t.source_revision_id=:revision'''),
            {'workflow': UUID(payload['workflow_run_id']), 'principal': context.principal_id,
             'project': UUID(payload['project_id']), 'revision': UUID(payload['revision_id'])})).mappings().one_or_none()
        if row is None:
            raise ApplicationError('工程输入与持久任务来源不一致', type='engineering_source_mismatch', non_retryable=True)
        for permission in engineering_permissions(row['task_kind']):
            await authorized_document(conn, context, row['document_id'], permission)
        sources=[(dict(row),'checkpoint.FCStd')]
        if row['task_kind']=='release_package':
            release=ReleaseTask.model_validate(row['request_payload']['engineering_task'])
            for reference in release.engineering_artifacts:
                artifact=(await conn.execute(text('''SELECT a.* FROM artifacts a JOIN document_engineering_tasks t ON t.workflow_run_id=a.workflow_run_id
                    JOIN workflow_runs w ON w.id=t.workflow_run_id AND w.status='succeeded'
                    WHERE a.id=:id AND a.project_id=:project AND a.revision_id=:revision AND t.document_id=:doc
                    AND t.source_sha256=:source_sha AND t.task_kind IN ('linear_static','contour_milling')'''),
                    {'id':reference.artifact_id,'project':UUID(payload['project_id']),'revision':UUID(payload['revision_id']),
                     'doc':row['document_id'],'source_sha':row['sha256']})).mappings().one_or_none()
                if artifact is None or artifact['sha256']!=reference.sha256 or artifact['size_bytes']!=reference.size_bytes or artifact['workflow_run_id']!=reference.workflow_run_id or artifact['artifact_kind']!=reference.artifact_kind:
                    raise ApplicationError('发布工程输入与保存的证据不一致',type='release_evidence_mismatch',non_retryable=True)
                sources.append((dict(artifact),reference.package_filename))
        if payload.get('input_artifacts') != [{'artifact_id':str(source['id']),'filename':filename} for source,filename in sources]:
            raise ApplicationError('工程输入与持久任务来源不一致',type='engineering_source_mismatch',non_retryable=True)
    declarations=[];materialized={}
    for source,filename in sources:
        path=Path(directory)/filename;measured=await download_object(source['object_key'],path)
        if measured['sha256']!=source['sha256'] or measured['size_bytes']!=source['size_bytes']:
            raise ApplicationError('工程输入完整性校验失败',type='engineering_source_integrity',non_retryable=True)
        declarations.append(ArtifactInput(artifact_id=str(source['id']),filename=filename,sha256=source['sha256'],
            size_bytes=source['size_bytes'],media_type=source['content_type']))
        materialized[str(source['id'])]=path
    return tuple(declarations),materialized


async def compute_engineering(payload, *, execute):
    context = SimpleNamespace(tenant_id=UUID(payload['tenant_id']), principal_id=UUID(payload['principal_id']))
    workflow_id = UUID(payload['workflow_run_id'])
    try:
        raw_task=payload['engineering_task']
        params = (ReleaseTask.model_validate(raw_task) if raw_task.get('kind')=='release_package' else ENGINEERING_TASK.validate_python(raw_task)).model_dump(mode='json')
        async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
            row = (await conn.execute(text('''SELECT w.request_payload,w.status,t.document_id FROM workflow_runs w
                JOIN document_engineering_tasks t ON t.workflow_run_id=w.id WHERE w.id=:id AND w.kind='mcad.engineering'
                AND t.principal_id=:principal FOR UPDATE OF w'''), {'id': workflow_id, 'principal': context.principal_id})).mappings().one()
            persisted = dict(row['request_payload'])
            if any(payload.get(k) != v for k, v in persisted.items()):
                raise ValueError('工程执行请求与已保存的不可变任务不同')
            for permission in engineering_permissions(params['kind']):
                await authorized_document(conn, context, row['document_id'], permission)
            status = WorkflowStatus(row['status'])
            for expected, target in ((WorkflowStatus.PENDING, WorkflowStatus.PLANNING), (WorkflowStatus.PLANNING, WorkflowStatus.RUNNING)):
                if status == expected:
                    await transition_workflow(conn, workflow_id, expected=expected, target=target)
                    status = target
            if status == WorkflowStatus.CANCELLING:
                raise ApplicationError('工程计算已请求取消', type='engineering_cancelled', non_retryable=True)
            if status not in {WorkflowStatus.RUNNING, WorkflowStatus.SUCCEEDED}:
                raise ValueError('工程任务已终止')
        outputs = {**engineering_outputs(params['kind']), 'capability-result': 'application/json'}
        references=params.get('engineering_artifacts',[]) if params['kind']=='release_package' else []
        native_inputs={'base':'checkpoint.FCStd',**{f'evidence_{i}':r['package_filename'] for i,r in enumerate(references)}}
        execution_payload = {**payload, 'revision_id': payload['source_revision_id'],
            'expected_base_revision_id': payload['source_revision_id'], 'step_index': 0,
            'input_artifacts': [{'artifact_id': payload['source_artifact_id'], 'filename': 'checkpoint.FCStd'},
                *[{'artifact_id':r['artifact_id'],'filename':r['package_filename']} for r in references]],
            'execution': {'step_key': 'engineering_compute', 'kind': 'engineering_compute', 'capability': 'mcad.freecad',
                'operation': 'engineering', 'mode': 'analysis', 'source_language': 'json', 'timeout_seconds': 300,
                'source_code': json.dumps({'schema_version': 'mcad-capability-task.v1', 'capability': 'freecad',
                    'operation': 'engineering', 'params': params, 'inputs': native_inputs}, sort_keys=True),
                'outputs': [{'name': name, 'media_type': media, 'max_size_bytes': 100 * 1024 * 1024} for name, media in outputs.items()]}}
        result = await execute(execution_payload)
        report_row = next(a for a in result['artifacts'] if a['artifact_kind'] == 'engineering_report')
        raw = await get_object(report_row['object_key'])
        if len(raw) != report_row['size_bytes'] or hashlib.sha256(raw).hexdigest() != report_row['sha256']:
            raise ValueError('工程结果完整性校验失败')
        report = json.loads(raw)
        if report.get('source_fcstd_sha256') != payload['source_sha256'] or report.get('kind') != params['kind'] or (
            params['kind']!='release_package' and report.get('component_name') != params['component_name']):
            raise ValueError('工程结果没有绑定正确的来源部件')
        async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
            for permission in engineering_permissions(params['kind']):
                await authorized_document(conn, context, UUID(payload['document_id']), permission)
            status = await conn.scalar(text('SELECT status FROM workflow_runs WHERE id=:id FOR UPDATE'), {'id': workflow_id})
            if status == WorkflowStatus.RUNNING.value:
                await transition_workflow(conn, workflow_id, expected=WorkflowStatus.RUNNING, target=WorkflowStatus.SUCCEEDED)
                await append_workflow_event(conn, tenant_id=context.tenant_id, workflow_id=workflow_id,
                    event_type='engineering.completed', payload={'source_revision_id': payload['source_revision_id'],
                        'source_sha256': payload['source_sha256'], 'kind': params['kind'], 'report_artifact_id': report_row['artifact_id']})
            elif status != WorkflowStatus.SUCCEEDED.value:
                raise ValueError('工程任务在结果发布前已终止或取消')
        return {**result, 'status': 'succeeded', 'source_revision_id': payload['source_revision_id']}
    except (KeyError, ValueError, PermissionError) as exc:
        raise ApplicationError(str(exc), type='engineering_request_invalid', non_retryable=True) from exc
