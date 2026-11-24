"""Owned, content-addressed files enter the ordinary native candidate workflow."""
import hashlib
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.projects import Permission
from app.models.schemas import ManufacturingProfile
from app.models.workflow_requests import NativeImportV1, OperationContextV1
from app.services.cloud_documents import DocumentConflict, authorized_document
from app.services.durable_submission import ensure_workspace_identity, recover_session_submission, submit_durable_workflow


async def import_document(principal, *, session_id: str, panel_id: str, filename: str,
                          payload: bytes, idempotency_key: str, manufacturing_profile: dict | None):
    idempotency_key = idempotency_key.strip()
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
    digest = hashlib.sha256(payload).hexdigest()
    source = NativeImportV1(format='fcstd' if suffix == '.fcstd' else 'step',
        artifact_id=uuid5(identity.branch_id, digest),
        sha256=digest,size_bytes=len(payload),filename=name)
    async with tenant_transaction(principal.tenant_id,principal.principal_id) as conn:
        doc = await authorized_document(conn,principal,identity.branch_id,Permission.MODIFY_DESIGN)
        prior = (await conn.execute(text('SELECT request_payload FROM workflow_runs WHERE tenant_id=:tenant AND requested_by_principal_id=:principal AND idempotency_key=:key'),
            {'tenant':principal.tenant_id,'principal':principal.principal_id,'key':idempotency_key})).scalar_one_or_none()
        if prior:
            if ((prior.get('operation_context') or {}).get('native_import') != source.model_dump(mode='json')
                    or prior.get('branch_id') != str(identity.branch_id)):
                raise DocumentConflict('原导入请求标识已绑定到不同的文件或文档')
            base, state_version = UUID(prior['expected_base_revision_id']), prior['expected_state_version']
        else:
            manifest = await conn.scalar(text('SELECT manifest FROM project_revisions WHERE id=:id'), {'id':doc['head_revision_id']})
            if not manifest or manifest.get('state') != 'empty':
                raise ValueError('请新建设计后导入；已有模型不能被导入文件覆盖')
            base, state_version = doc['head_revision_id'], doc['state_version']
    context = OperationContextV1(rule='explicit_native_import',source_channel='rest',resolved_operation='generate',
        requested_operation='generate',submission_modeling_backend='freecad',base_revision_id=base,
        base_source_kind='native_import_artifact',base_source_id=source.artifact_id,base_source_sha256=source.sha256,native_import=source)
    await submit_durable_workflow(principal,project_id=identity.project_id,branch_id=identity.branch_id,
        expected_base_revision_id=base,expected_state_version=state_version,idempotency_key=idempotency_key,
        operation='generate',objective=f'导入原生工程文件 {name}',output_formats=['step','stl'],
        manufacturing_profile=profile,modeling_backend='freecad',operation_context=context, native_import_bytes=payload)
    receipt = await recover_session_submission(principal, session_id=session_id, panel_id=panel_id,
        idempotency_key=idempotency_key)
    if receipt is None:
        raise RuntimeError("无法读取已受理的导入任务")
    return receipt
