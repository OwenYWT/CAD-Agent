"""Named releases bind native geometry, BOM and selected engineering evidence."""
from uuid import UUID
from sqlalchemy import text
from app.db import tenant_transaction
from app.domain.projects import Permission
from app.execution.canonical import canonical_sha256
from app.freecad.engineering_contracts import engineering_outputs
from app.freecad.release_contracts import ReleaseSubmission,ReleaseTask
from app.services.cloud_documents import DocumentConflict,authorized_document,checkpoint
from app.services.document_engineering import queue_engineering,engineering_result
from app.services.feature_annotations import annotation_context
from app.services.run_state import IdempotencyConflict
from app.workflows.temporal import _dispatch_after_commit


async def submit_release(context,document_id,submission:ReleaseSubmission):
    name=submission.release_name.strip()
    request={'document_id':str(document_id),'release_name':name,'revision_id':str(submission.expected_revision_id),
        'state_version':submission.expected_state_version,'engineering_workflow_ids':sorted(map(str,submission.engineering_workflow_ids))}
    from app.freecad.release_contracts import ReleaseOptions
    if submission.options != ReleaseOptions():
        request['options'] = submission.options.model_dump(mode='json')
    digest=canonical_sha256(request);key='release:'+submission.idempotency_key;dispatch=None
    projection=await checkpoint(context,document_id,submission.expected_revision_id)
    if not projection['fcstd']:
        raise ValueError('发布需要已提交的原生模型')
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        doc=await authorized_document(conn,context,document_id,Permission.COMMIT_VERSION)
        await authorized_document(conn,context,document_id,Permission.EXPORT_ARTIFACT)
        await conn.execute(text('SELECT id FROM projects WHERE id=:id FOR UPDATE'),{'id':doc['project_id']})
        previous=(await conn.execute(text('SELECT * FROM document_engineering_tasks WHERE principal_id=:principal AND idempotency_key=:key'),
            {'principal':context.principal_id,'key':key})).mappings().one_or_none()
        if previous is not None:
            if previous['request_hash']!=digest or previous['task_kind']!='release_package':
                raise IdempotencyConflict('发布请求 ID 已用于其他内容')
            workflow_id=previous['workflow_run_id']
        else:
            doc=await authorized_document(conn,context,document_id,Permission.COMMIT_VERSION,lock=True)
            if doc['head_revision_id']!=submission.expected_revision_id or doc['state_version']!=submission.expected_state_version:
                raise DocumentConflict('模型已更新，请重新核对发布来源')
            if await conn.scalar(text('SELECT workflow_run_id FROM document_releases WHERE document_id=:doc AND release_name=:name'),{'doc':document_id,'name':name}):
                raise DocumentConflict('当前文档已使用此发布名称，请选择新的名称')
            source=(await conn.execute(text('''SELECT a.*,r.source_workflow_run_id FROM artifacts a JOIN project_revisions r ON r.id=a.revision_id
                WHERE a.id=:id AND a.revision_id=:revision AND r.branch_id=:doc'''),
                {'id':UUID(projection['fcstd']['artifact_id']),'revision':submission.expected_revision_id,'doc':document_id})).mappings().one()
            references=[]
            for analysis_id in sorted(submission.engineering_workflow_ids,key=str):
                analysis=(await conn.execute(text('''SELECT t.*,w.status FROM document_engineering_tasks t JOIN workflow_runs w ON w.id=t.workflow_run_id
                    WHERE t.workflow_run_id=:id AND t.document_id=:doc'''),{'id':analysis_id,'doc':document_id})).mappings().one_or_none()
                if analysis is None or analysis['status']!='succeeded' or analysis['task_kind'] not in {'linear_static','contour_milling','native_measure'}:
                    raise ValueError('只能选择本文件中已成功完成的有限元或加工任务')
                if analysis['source_revision_id']!=submission.expected_revision_id or analysis['source_sha256']!=source['sha256']:
                    raise ValueError('所选工程证据属于其他修订或原生检查点，请重新计算后发布')
                kinds=engineering_outputs(analysis['task_kind'])
                artifacts=[a for a in (await conn.execute(text('SELECT * FROM artifacts WHERE workflow_run_id=:id ORDER BY artifact_kind'),
                    {'id':analysis_id})).mappings() if a['artifact_kind'] in kinds]
                if len(artifacts)!=len(kinds) or {a['artifact_kind'] for a in artifacts}!=set(kinds):
                    raise ValueError('所选工程任务没有完整的不可变工件')
                for artifact in artifacts:
                    kind=artifact['artifact_kind'];extension='zip' if kind=='engineering_bundle' else 'nc' if kind=='cam_program' else 'json'
                    references.append({'artifact_id':artifact['id'],'workflow_run_id':analysis_id,'source_revision_id':submission.expected_revision_id,
                        'artifact_kind':kind,'package_filename':f'{analysis_id.hex}-{kind}.{extension}',
                        'size_bytes':artifact['size_bytes'],'sha256':artifact['sha256']})
            if source['size_bytes']+sum(r['size_bytes'] for r in references)>128*1024*1024:
                raise ValueError('所选发布输入超过 128 MiB 预算')
            source_request = await conn.scalar(text('SELECT request_payload FROM workflow_runs WHERE id=:id AND project_id=:project'), {'id': source['source_workflow_run_id'], 'project': doc['project_id']})
            requirements = await conn.scalar(text("SELECT payload->'requirements' FROM task_events WHERE workflow_run_id=:id AND event_type='agent.requirements.completed' ORDER BY sequence LIMIT 1"), {'id': source['source_workflow_run_id']})
            task=ReleaseTask(release_name=name,options=submission.options,
                manufacturing_profile=(source_request or {}).get('manufacturing_profile'), requirements=requirements,
                source={'document_id':document_id,'project_id':doc['project_id'],
                'revision_id':submission.expected_revision_id,'state_version':submission.expected_state_version,
                'fcstd_artifact_id':source['id'],'fcstd_sha256':source['sha256']},engineering_artifacts=references,
                annotations=await annotation_context(conn,document_id))
            workflow_id,dispatch=await queue_engineering(conn,context,doc,source,task.model_dump(mode='json'),
                idempotency_key=key,request_hash=digest)
            await conn.execute(text('INSERT INTO document_releases(workflow_run_id,tenant_id,document_id,release_name) VALUES(:workflow,:tenant,:doc,:name)'),
                {'workflow':workflow_id,'tenant':context.tenant_id,'doc':document_id,'name':name})
    if dispatch is not None:await _dispatch_after_commit(dispatch)
    return {'workflow_run_id':str(workflow_id),'release_id':str(workflow_id),'tenant_id':str(context.tenant_id),'replayed':previous is not None}


async def document_releases(context,document_id):
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        await authorized_document(conn,context,document_id)
        rows=(await conn.execute(text('''SELECT r.workflow_run_id AS release_id,r.release_name,r.created_at,t.source_revision_id,t.source_state_version,
            w.status,w.error_code,w.error_message FROM document_releases r JOIN document_engineering_tasks t ON t.workflow_run_id=r.workflow_run_id
            JOIN workflow_runs w ON w.id=r.workflow_run_id WHERE r.document_id=:doc ORDER BY r.created_at DESC,r.workflow_run_id DESC LIMIT 50'''),
            {'doc':document_id})).mappings().all()
        return {'releases':[dict(r) for r in rows]}


async def release_result(context,document_id,release_id):
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        await authorized_document(conn,context,document_id)
        if not await conn.scalar(text('SELECT workflow_run_id FROM document_releases WHERE workflow_run_id=:id AND document_id=:doc'),
            {'id':release_id,'doc':document_id}):raise KeyError(release_id)
    return await engineering_result(context,document_id,release_id)
