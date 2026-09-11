"""Revision-backed branches and semantic merge proposals, with durable execution."""
from __future__ import annotations

import json
from uuid import UUID, uuid4

from sqlalchemy import text

from app.config import settings
from app.db import tenant_transaction
from app.domain.projects import Permission
from app.execution.canonical import canonical_sha256
from app.repositories.revisions import create_initial_branch
from app.services.cloud_documents import DocumentConflict, authorized_document, checkpoint
from app.services.run_state import create_workflow, IdempotencyConflict
from app.services.workflow_dispatch import persist_dispatch
from app.services.document_diff import common_ancestor, semantic_changes, parameter_merge_proposal
from app.services.document_rebase import ParameterRebaseConflict
from app.services.feature_annotations import annotation_context, annotate_projection
from app.freecad.state_contract import read_verified_state_artifact, compile_parameter_operation_plan
from app.workflows.temporal import (FreeCADRevisionRestoreV1, FreeCADStructuredModificationV1,
    McadAgentWorkflowV2Request, OperationContextV1, mcad_agent_v2_request_payload,
    temporal_agent_v2_workflow_id, _dispatch_after_commit)


async def _lineage(conn, document_id):
    return await conn.scalar(text('SELECT lineage_id FROM document_forks WHERE document_id=:id'), {'id':document_id}) or document_id


async def list_document_branches(context, document_id):
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        doc = await authorized_document(conn, context, document_id)
        lineage = await _lineage(conn, document_id)
        rows = (await conn.execute(text("""SELECT d.id AS document_id,b.name,d.head_revision_id,d.state_version,
            f.source_document_id,f.source_revision_id,f.workflow_run_id,w.status AS preparation_status
            FROM cloud_documents d JOIN project_branches b ON b.id=d.id
            LEFT JOIN document_forks f ON f.document_id=d.id LEFT JOIN workflow_runs w ON w.id=f.workflow_run_id
            WHERE d.project_id=:project AND COALESCE(f.lineage_id,d.id)=:lineage ORDER BY b.created_at,d.id"""),
            {'project':doc['project_id'],'lineage':lineage})).mappings().all()
        return {'tenant_id':str(context.tenant_id),'lineage_id':str(lineage),'branches':[dict(row) for row in rows]}


async def _queue(conn, context, doc, *, key, objective, rule, source, restore=None, modification=None):
    operation_context = OperationContextV1(rule=rule, source_channel='rest', requested_operation='modify',
        resolved_operation='modify', submission_modeling_backend='freecad', base_revision_id=doc['head_revision_id'],
        base_source_kind='fcstd_artifact', base_source_id=UUID(source['artifact_id']), base_source_sha256=source['sha256'])
    payload = mcad_agent_v2_request_payload(branch_id=doc['id'],expected_base_revision_id=doc['head_revision_id'],
        operation='modify',objective=objective,existing_code=None,manufacturing_profile=None,output_formats=('step','stl'),
        confirmation_timeout_seconds=3600,modeling_backend='freecad',operation_context=operation_context,
        structured_modification=modification,revision_restore=restore,expected_state_version=doc['state_version'])
    created = await create_workflow(conn,tenant_id=context.tenant_id,project_id=doc['project_id'],
        requested_by_principal_id=context.principal_id,kind='mcad.agent.v2.modify',idempotency_key=key,request_payload=payload)
    request = McadAgentWorkflowV2Request(workflow_run_id=created.workflow_id,tenant_id=context.tenant_id,
        project_id=doc['project_id'],principal_id=context.principal_id,branch_id=doc['id'],
        expected_base_revision_id=doc['head_revision_id'],expected_state_version=doc['state_version'],document_queue=True,
        operation='modify',objective=objective,modeling_backend='freecad',operation_context=operation_context,
        structured_modification=modification,revision_restore=restore)
    dispatch = await persist_dispatch(conn,tenant_id=context.tenant_id,principal_id=context.principal_id,
        workflow_id=created.workflow_id,workflow_type='McadAgentWorkflowV2',temporal_id=temporal_agent_v2_workflow_id(created.workflow_id),
        task_queue=settings.temporal_agent_v2_task_queue,payload=request.temporal_payload())
    return created.workflow_id, dispatch


async def _inherit_annotations(conn, context, source_id, document_id, revision_id):
    annotations = (await conn.execute(text("""SELECT DISTINCT ON(feature_id) * FROM feature_annotations
        WHERE document_id=:source ORDER BY feature_id,version DESC"""), {'source':source_id})).mappings().all()
    for annotation in annotations:
        sequence = await conn.scalar(text('UPDATE cloud_documents SET event_sequence=event_sequence+1 WHERE id=:doc RETURNING event_sequence'), {'doc':document_id})
        await conn.execute(text("""INSERT INTO feature_annotations(id,tenant_id,document_id,feature_id,kernel_name,
            revision_id,principal_id,version,event_sequence,role,intent)
            VALUES(:id,:tenant,:doc,:feature,:name,:revision,:principal,:version,:sequence,:role,:intent)"""),
            {'id':uuid4(),'tenant':context.tenant_id,'doc':document_id,'feature':annotation['feature_id'],
             'name':annotation['kernel_name'],'revision':revision_id,'principal':annotation['principal_id'],
             'version':annotation['version'],'sequence':sequence,'role':annotation['role'],'intent':annotation['intent']})
        await conn.execute(text("""INSERT INTO document_events(tenant_id,document_id,sequence,event_type,payload)
            VALUES(:tenant,:doc,:sequence,'feature.annotated',CAST(:payload AS jsonb))"""),
            {'tenant':context.tenant_id,'doc':document_id,'sequence':sequence,'payload':json.dumps({
                'feature_id':str(annotation['feature_id']),'role':annotation['role'],'intent':annotation['intent'],
                'annotation_version':annotation['version'],'annotation_source':'user','inherited_from_annotation_id':str(annotation['id'])})})


async def fork_document(context, document_id, *, name, expected_revision_id, expected_state_version, idempotency_key):
    name = name.strip()
    if not name or len(name) > 120:
        raise ValueError('分支名称需要 1 至 120 个字符')
    digest = canonical_sha256({'document_id':str(document_id),'name':name,'revision_id':str(expected_revision_id),
        'state_version':expected_state_version})
    source = await checkpoint(context, document_id, expected_revision_id)
    if source['fcstd'] is None:
        raise ValueError('创建云 CAD 分支需要原生 FCStd 检查点')
    dispatch = None
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        doc = await authorized_document(conn, context, document_id, Permission.MODIFY_DESIGN)
        await conn.execute(text('SELECT id FROM projects WHERE id=:id FOR UPDATE'), {'id':doc['project_id']})
        existing = (await conn.execute(text('SELECT * FROM document_forks WHERE principal_id=:principal AND idempotency_key=:key'),
            {'principal':context.principal_id,'key':idempotency_key})).mappings().one_or_none()
        if existing is not None:
            if existing['request_hash'] != digest:
                raise IdempotencyConflict('分支请求 ID 已用于其他内容')
            result = {'document_id':str(existing['document_id']),'workflow_run_id':str(existing['workflow_run_id']),'replayed':True}
        else:
            doc = await authorized_document(conn, context, document_id, Permission.MODIFY_DESIGN, lock=True)
            if doc['head_revision_id'] != expected_revision_id or doc['state_version'] != expected_state_version:
                raise DocumentConflict('来源文档已更新，请刷新后创建分支')
            if await conn.scalar(text('SELECT id FROM project_branches WHERE project_id=:project AND name=:name'), {'project':doc['project_id'],'name':name}):
                raise DocumentConflict('此项目已存在同名分支')
            lineage = await _lineage(conn, document_id)
            initial = await create_initial_branch(conn,tenant_id=context.tenant_id,project_id=doc['project_id'],
                created_by_principal_id=context.principal_id,branch_name=name,initial_manifest={
                    'fork_source_revision_id':str(expected_revision_id),'fork_source_document_id':str(document_id),'lineage_id':str(lineage)})
            await conn.execute(text("""INSERT INTO document_forks(document_id,tenant_id,project_id,source_document_id,
                source_revision_id,source_state_version,seed_revision_id,lineage_id,principal_id,idempotency_key,request_hash)
                VALUES(:doc,:tenant,:project,:source,:revision,:version,:seed,:lineage,:principal,:key,:hash)"""),
                {'doc':initial.branch_id,'tenant':context.tenant_id,'project':doc['project_id'],'source':document_id,
                 'revision':expected_revision_id,'version':expected_state_version,'seed':initial.revision_id,'lineage':lineage,
                 'principal':context.principal_id,'key':idempotency_key,'hash':digest})
            await _inherit_annotations(conn,context,document_id,initial.branch_id,initial.revision_id)
            target = await authorized_document(conn,context,initial.branch_id,Permission.MODIFY_DESIGN)
            workflow_id, dispatch = await _queue(conn,context,target,key=f'fork:{context.principal_id}:{idempotency_key}',
                objective=f'从修订 {expected_revision_id} 创建分支 {name}',rule='explicit_branch_fork',source=source['fcstd'],
                restore=FreeCADRevisionRestoreV1(source_revision_id=expected_revision_id,
                    source_artifact_id=UUID(source['fcstd']['artifact_id']),source_sha256=source['fcstd']['sha256']))
            await conn.execute(text('UPDATE document_forks SET workflow_run_id=:workflow WHERE document_id=:doc'),
                {'workflow':workflow_id,'doc':initial.branch_id})
            result = {'document_id':str(initial.branch_id),'workflow_run_id':str(workflow_id),'replayed':False}
    if dispatch is not None:
        await _dispatch_after_commit(dispatch)
    return {**result,'tenant_id':str(context.tenant_id)}


async def compare_branches(context, document_id, source_document_id):
    if document_id == source_document_id:
        raise ValueError('请选择其他来源分支')
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        target = await authorized_document(conn,context,document_id)
        source = await authorized_document(conn,context,source_document_id)
        if target['project_id'] != source['project_id'] or await _lineage(conn,document_id) != await _lineage(conn,source_document_id):
            raise ValueError('只能比较同一模型谱系的分支')
        rows = (await conn.execute(text("""SELECT r.id,r.branch_id,r.parent_revision_id,r.source_workflow_run_id,
            jsonb_build_object('structured_modification',o.arguments->'structured_modification',
                'operation_context',o.arguments->'operation_context','revision_restore',o.arguments->'revision_restore') AS arguments,
            o.action,f.source_revision_id AS branch_source_revision,f.workflow_run_id AS fork_workflow,
            CASE WHEN r.id=f.seed_revision_id THEN f.source_revision_id END AS fork_source,
            m.source_revision_id AS merge_source FROM project_revisions r
            LEFT JOIN cad_operations o ON o.id=r.source_workflow_run_id
            LEFT JOIN document_forks f ON f.document_id=r.branch_id
            LEFT JOIN document_merges m ON m.workflow_run_id=r.source_workflow_run_id
            WHERE r.project_id=:project ORDER BY r.created_at,r.id LIMIT 10001"""),
            {'project':target['project_id']})).mappings().all()
        if len(rows) > 10000:
            raise ValueError('项目历史超过本次比较上限，需要缩小历史范围')
        nodes = {r['id']:dict(r) for r in rows}
        common = common_ancestor(nodes,target['head_revision_id'],source['head_revision_id'])
        source_annotations = await annotation_context(conn,source_document_id,through_sequence=source['event_sequence'])
        target_annotations = await annotation_context(conn,document_id,through_sequence=target['event_sequence'])
    base = await checkpoint(context,nodes[common]['branch_id'],common)
    left = await checkpoint(context,document_id,target['head_revision_id'])
    right = await checkpoint(context,source_document_id,source['head_revision_id'])
    if not all(p['fcstd'] and p['state'] for p in (base,left,right)):
        raise ValueError('请先完成分支模型校验与提交，再比较原生检查点')
    conflicts = []
    try:
        parameter_proposal = parameter_merge_proposal(nodes,common,source['head_revision_id'],base,right,left)
    except ParameterRebaseConflict as exc:
        parameter_proposal = {'updates':[], 'proof':None, 'reason':str(exc)}
        conflicts.append({'code':exc.code,'message':str(exc)})
    result = {'document_id':str(document_id),'source_document_id':str(source_document_id),
        'target_revision_id':str(target['head_revision_id']),'target_state_version':target['state_version'],
        'source_revision_id':str(source['head_revision_id']),'source_state_version':source['state_version'],
        'common_revision_id':str(common),'source_changes':semantic_changes(base,right),
        'target_changes':semantic_changes(base,left),
        'differences':semantic_changes(annotate_projection(left,target_annotations),annotate_projection(right,source_annotations)),
        'parameters':parameter_proposal,'conflicts':conflicts,
        'can_merge_parameters':bool(parameter_proposal['updates']) and not conflicts,
        'annotation_policy':'保留目标分支的用途与意图；来源标注差异仅供审阅，不自动覆盖。',
        'source_geometry_policy':'采用来源分支的完整几何，替换目标几何；重新校验并审核提交。'}
    return {**result,'comparison_hash':canonical_sha256(result)}


async def merge_document(context, document_id, *, source_document_id, source_revision_id, source_state_version,
                         target_revision_id, target_state_version, mode, comparison_hash, idempotency_key):
    if mode not in {'parameters','source_geometry'}:
        raise ValueError('未知合并模式')
    digest = canonical_sha256({'document_id':str(document_id),'source_document_id':str(source_document_id),
        'source_revision_id':str(source_revision_id),'source_state_version':source_state_version,
        'target_revision_id':str(target_revision_id),'target_state_version':target_state_version,
        'mode':mode,'comparison_hash':comparison_hash})
    async def replay(conn):
        existing = (await conn.execute(text('SELECT * FROM document_merges WHERE principal_id=:principal AND idempotency_key=:key'),
            {'principal':context.principal_id,'key':idempotency_key})).mappings().one_or_none()
        if existing is None:
            return None
        if existing['request_hash'] != digest:
            raise IdempotencyConflict('合并请求 ID 已用于其他内容')
        return {'merge_id':str(existing['id']),'workflow_run_id':str(existing['workflow_run_id']),
            'document_id':str(document_id),'replayed':True}
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        await authorized_document(conn,context,document_id,Permission.MODIFY_DESIGN)
        await authorized_document(conn,context,source_document_id)
        existing = await replay(conn)
        if existing:
            return existing
    comparison = await compare_branches(context,document_id,source_document_id)
    if comparison['comparison_hash'] != comparison_hash or (
        comparison['source_revision_id'],comparison['source_state_version'],comparison['target_revision_id'],comparison['target_state_version']
    ) != (str(source_revision_id),source_state_version,str(target_revision_id),target_state_version):
        raise DocumentConflict('分支内容已变化，请重新比较后提交合并')
    if mode == 'parameters' and not comparison['can_merge_parameters']:
        raise DocumentConflict(comparison['parameters']['reason'] or '参数合并存在冲突')
    source = await checkpoint(context,source_document_id if mode=='source_geometry' else document_id,
        source_revision_id if mode=='source_geometry' else target_revision_id)
    restore = modification = None
    if mode == 'source_geometry':
        restore = FreeCADRevisionRestoreV1(source_revision_id=source_revision_id,
            source_artifact_id=UUID(source['fcstd']['artifact_id']),source_sha256=source['fcstd']['sha256'])
    else:
        modification = FreeCADStructuredModificationV1(expected_state_sha256=source['parameter_state_sha256'],
            parameter_updates=comparison['parameters']['updates'])
        async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
            rows = [dict(a) for a in (await conn.execute(text('SELECT * FROM artifacts WHERE id=:id'),
                {'id':UUID(source['state']['artifact_id'])})).mappings()]
        state, _ = await read_verified_state_artifact(rows,require_v2=True,expected_sha256=source['parameter_state_sha256'])
        compile_parameter_operation_plan(state,modification.model_dump(mode='json'),output_formats=('step','stl'))
    dispatch = None
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        target = await authorized_document(conn,context,document_id,Permission.MODIFY_DESIGN)
        await conn.execute(text('SELECT id FROM projects WHERE id=:id FOR UPDATE'), {'id':target['project_id']})
        for key in sorted((document_id,source_document_id)):
            await authorized_document(conn,context,key,Permission.MODIFY_DESIGN if key==document_id else Permission.VIEW_PROJECT,lock=True)
        existing = await replay(conn)
        if existing:
            return existing
        target = await authorized_document(conn,context,document_id,Permission.MODIFY_DESIGN)
        branch = await authorized_document(conn,context,source_document_id)
        if (target['head_revision_id'],target['state_version'],branch['head_revision_id'],branch['state_version']) != (
            target_revision_id,target_state_version,source_revision_id,source_state_version):
            raise DocumentConflict('分支已更新，请重新比较')
        merge_id = uuid4()
        await conn.execute(text("""INSERT INTO document_merges(id,tenant_id,project_id,document_id,source_document_id,
            source_revision_id,source_state_version,target_revision_id,target_state_version,common_revision_id,mode,
            principal_id,idempotency_key,request_hash,proposal) VALUES(:id,:tenant,:project,:doc,:source,:source_rev,
            :source_version,:target_rev,:target_version,:common,:mode,:principal,:key,:hash,CAST(:proposal AS jsonb))"""),
            {'id':merge_id,'tenant':context.tenant_id,'project':target['project_id'],'doc':document_id,'source':source_document_id,
             'source_rev':source_revision_id,'source_version':source_state_version,'target_rev':target_revision_id,
             'target_version':target_state_version,'common':UUID(comparison['common_revision_id']),'mode':mode,
             'principal':context.principal_id,'key':idempotency_key,'hash':digest,'proposal':json.dumps(comparison)})
        workflow_id, dispatch = await _queue(conn,context,target,key=f'merge:{context.principal_id}:{idempotency_key}',
            objective=('合并独立参数变更' if mode=='parameters' else '采用来源分支完整几何')+f'：{source_revision_id}',
            rule='explicit_branch_merge',source=source['fcstd'],restore=restore,modification=modification)
        await conn.execute(text('UPDATE document_merges SET workflow_run_id=:workflow WHERE id=:id'),
            {'workflow':workflow_id,'id':merge_id})
    await _dispatch_after_commit(dispatch)
    return {'merge_id':str(merge_id),'workflow_run_id':str(workflow_id),'document_id':str(document_id),'replayed':False}
