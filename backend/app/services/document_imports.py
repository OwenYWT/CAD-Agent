"""Owned, content-addressed files enter the ordinary native candidate workflow."""
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.projects import Permission
from app.models.schemas import ManufacturingProfile
from app.models.workflow_requests import NativeImportV1, OperationContextV1
from app.object_store import put_object
from app.services.cloud_documents import authorized_document
from app.services.durable_submission import ensure_workspace_identity, submit_durable_workflow


async def import_document(principal, *, session_id: str, panel_id: str, filename: str,
                          payload: bytes, idempotency_key: str, manufacturing_profile: dict | None):
    import hashlib
    if not (1 <= len(session_id) <= 500 and 1 <= len(panel_id) <= 500 and 1 <= len(idempotency_key) <= 120):
        raise ValueError('导入请求身份无效')
    name = Path(filename.replace('\\', '/')).name
    suffix = Path(name).suffix.lower()
    if suffix not in {'.fcstd', '.step', '.stp'}:
        raise ValueError('支持 FCStd、STEP 和 STP 文件')
    if not 0 < len(payload) <= 64*1024*1024 or len(name) > 240:
        raise ValueError('导入文件为空或超过 64 MiB')
    profile = ManufacturingProfile.model_validate(manufacturing_profile) if manufacturing_profile else None
    identity = await ensure_workspace_identity(principal,session_id=session_id,panel_id=panel_id,
        title=f'导入 {name}',user_id=principal.external_subject.removeprefix('user:') if principal.external_subject.startswith('user:') else None)
    source = NativeImportV1(format='fcstd' if suffix == '.fcstd' else 'step',
        artifact_id=uuid5(identity.branch_id, hashlib.sha256(payload).hexdigest()),
        sha256=hashlib.sha256(payload).hexdigest(),size_bytes=len(payload),filename=name)
    async with tenant_transaction(principal.tenant_id,principal.principal_id) as conn:
        doc = await authorized_document(conn,principal,identity.branch_id,Permission.MODIFY_DESIGN)
        prior = (await conn.execute(text('SELECT request_payload FROM workflow_runs WHERE tenant_id=:tenant AND requested_by_principal_id=:principal AND idempotency_key=:key'),
            {'tenant':principal.tenant_id,'principal':principal.principal_id,'key':idempotency_key})).scalar_one_or_none()
        if prior:
            frozen = OperationContextV1.model_validate(prior.get('operation_context') or {})
            if frozen.native_import != source or prior.get('branch_id') != str(identity.branch_id):
                raise ValueError('原导入请求标识已绑定到不同的文件或文档')
            base, state_version = UUID(prior['expected_base_revision_id']), prior['expected_state_version']
        else:
            manifest = await conn.scalar(text('SELECT manifest FROM project_revisions WHERE id=:id'), {'id':doc['head_revision_id']})
            if not manifest or manifest.get('state') != 'empty':
                raise ValueError('请新建设计后导入；已有模型不能被导入文件覆盖')
            base, state_version = doc['head_revision_id'], doc['state_version']
    await put_object(source.object_key(principal.tenant_id,identity.branch_id),payload,
        content_type='application/x-freecad' if source.format=='fcstd' else 'model/step')
    context = OperationContextV1(rule='explicit_native_import',source_channel='rest',resolved_operation='generate',
        requested_operation='generate',submission_modeling_backend='freecad',base_revision_id=base,
        base_source_kind='native_import_artifact',base_source_id=source.artifact_id,base_source_sha256=source.sha256,native_import=source)
    submitted = await submit_durable_workflow(principal,project_id=identity.project_id,branch_id=identity.branch_id,
        expected_base_revision_id=base,expected_state_version=state_version,idempotency_key=idempotency_key,
        operation='generate',objective=f'导入原生工程文件 {name}',output_formats=['step','stl'],
        manufacturing_profile=profile,modeling_backend='freecad',operation_context=context)
    return {'workflow_run_id':str(submitted.workflow_run_id),'project_id':str(identity.project_id),
        'branch_id':str(identity.branch_id),'expected_base_revision_id':str(base),'panel_id':panel_id,
        'submission_id':idempotency_key,'status':'pending'}
