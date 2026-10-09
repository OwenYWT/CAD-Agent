"""Resolve an immutable reviewed candidate independently of the target saved head."""
from uuid import UUID

from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.projects import Permission
from app.repositories.artifacts import committed_artifact_for_revision
from app.services.cloud_documents import authorized_document, checkpoint
from app.services.event_relay import workflow_project_id


async def continuation_artifact(principal, project_id: UUID, branch_id: UUID,
                                saved_revision: UUID, source_revision: UUID) -> dict:
    async with tenant_transaction(principal.tenant_id, principal.principal_id) as connection:
        document = await authorized_document(connection, principal, branch_id, Permission.MODIFY_DESIGN)
        if document['project_id'] != project_id or document['head_revision_id'] != saved_revision:
            raise ValueError('已保存版本已变化，请重新确认续改基线')
        candidate = (await connection.execute(text('''
            SELECT c.base_revision_id, c.status
            FROM change_sets c JOIN project_revisions r
              ON r.tenant_id=c.tenant_id AND r.id=c.candidate_revision_id
            WHERE c.tenant_id=:tenant AND c.project_id=:project
              AND c.branch_id=:branch AND c.candidate_revision_id=:source
        '''), {'tenant': principal.tenant_id, 'project': project_id,
               'branch': branch_id, 'source': source_revision})).mappings().one_or_none()
        if candidate is None or candidate['status'] != 'changes_requested':
            raise ValueError('只能继续处理已明确请求修改的候选')
        if candidate['base_revision_id'] != saved_revision:
            raise ValueError('候选原始基线已变化，或缺少原生建模来源')
        artifact = await committed_artifact_for_revision(connection,
            tenant_id=principal.tenant_id, project_id=project_id,
            revision_id=source_revision, artifact_kind='fcstd')
        if artifact is None:
            raise ValueError('候选没有有效的原生检查点')
        return artifact

from app.services.task_diagnostics import failure_diagnostic


async def takeover_baseline(principal, workflow_id: UUID) -> dict:
    await workflow_project_id(principal, workflow_id, permission=Permission.MODIFY_DESIGN)
    diagnostic = await failure_diagnostic(principal, workflow_id)
    if diagnostic['task_status'] not in {'failed', 'cancelled', 'timed_out'}:
        raise ValueError('请先停止任务，等待服务器确认终止后再接管')
    document_id, revision_id = UUID(diagnostic['document_id']), UUID(diagnostic['base_revision_id'])
    async with tenant_transaction(principal.tenant_id, principal.principal_id) as conn:
        doc = await authorized_document(conn, principal, document_id, Permission.MODIFY_DESIGN, lock=True)
        if doc['head_revision_id'] != revision_id or diagnostic.get('base_state_version') != doc['state_version']:
            raise ValueError('原基线已变化，请在最新版本重新确认修改范围')
        busy = await conn.scalar(text("SELECT EXISTS(SELECT 1 FROM cad_operations WHERE document_id=:doc AND status IN ('queued','running'))"), {'doc': document_id})
        leased = await conn.scalar(text('SELECT EXISTS(SELECT 1 FROM document_feature_leases WHERE document_id=:doc AND expires_at>CURRENT_TIMESTAMP)'), {'doc': document_id})
        if busy or leased:
            raise ValueError('仍有运行任务或编辑租约，请等待释放后接管')
    projection = await checkpoint(principal, document_id, revision_id)
    return {'document_id': str(document_id), 'revision_id': str(revision_id),
            'state_version': doc['state_version'], 'diagnostic_sha256': diagnostic['sha256'],
            'sketches': [{'name': sketch['name'], 'feature_id': next((f['id'] for f in projection['features'] if f['kernel_name'] == sketch['name']), None)} for sketch in diagnostic['snapshot']['sketches']],
            'mode': 'valid_baseline', 'valid_checkpoint': False}
