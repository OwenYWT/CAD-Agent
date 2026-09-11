"""Serialize lease decisions under the document row; the kernel queue stays serial."""
from uuid import UUID,uuid4

from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.projects import Permission


class FeatureLeaseConflict(ValueError):
    code='feature_lease_conflict'


def ancestors(features, start):
    by_id={f['id']:f for f in features}
    seen=set();pending=[start]
    while pending:
        node=pending.pop()
        if node in seen:
            continue
        seen.add(node)
        pending.extend(by_id.get(node,{}).get('dependencies',[]))
    return seen


def related(features, left, right):
    return left in ancestors(features,right) or right in ancestors(features,left)


def operation_targets(features,payload):
    modification=payload.get('structured_modification')
    if not modification:
        return None
    native = modification.get('native_edits') or []
    if native:
        if any(edit.get('action') != 'sketch.set_constraint' for edit in native):
            return None
        names = {(edit.get('args') or {}).get('sketch') for edit in native}
    else:
        names={p['parameter_id'].split('.',1)[0] for p in modification.get('parameter_updates',[])}
    targets={f['id'] for f in features if f['kernel_name'] in names}
    return targets if targets and len(targets)==len(names) else None


async def assert_operation_lease_access(conn,document_id,principal_id,payload):
    """Called inside the atomic enqueue transaction, after locking the document."""
    active=(await conn.execute(text("SELECT * FROM document_feature_leases WHERE document_id=:doc AND expires_at>CURRENT_TIMESTAMP"),
                              {'doc':document_id})).mappings().all()
    token=(payload.get('operation_context') or {}).get('feature_lease_token')
    if token and not any(str(row['token'])==str(token) and row['principal_id']==principal_id for row in active):
        raise FeatureLeaseConflict('编辑租约已失效，请重新获取后提交')
    if not active:
        return
    projection=await conn.scalar(text("""SELECT projection FROM document_checkpoints c JOIN cloud_documents d
        ON d.id=c.document_id AND d.head_revision_id=c.revision_id WHERE d.id=:doc"""),{'doc':document_id})
    features=(projection or {}).get('features',[])
    targets=operation_targets(features,payload)
    for row in active:
        if str(row['token'])==str(token) and row['principal_id']==principal_id:
            continue
        if targets is None or any(related(features,target,str(row['feature_id'])) for target in targets):
            raise FeatureLeaseConflict('目标特征或依赖正由另一个编辑会话占用，请等待释放后重试')


async def acquire_feature_lease(context,document_id,*,feature_id,client_id,revision_id,lease_token=None):
    from app.services.cloud_documents import authorized_document,checkpoint,DocumentConflict
    projection=await checkpoint(context,document_id,revision_id)
    features=projection['features']
    if not any(f['id']==str(feature_id) for f in features):
        raise ValueError('特征不属于此文档版本')
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        doc=await authorized_document(conn,context,document_id,Permission.MODIFY_DESIGN,lock=True)
        if doc['head_revision_id']!=revision_id:
            raise DocumentConflict('文档已更新，请读取当前特征后编辑')
        await conn.execute(text('DELETE FROM document_feature_leases WHERE document_id=:doc AND expires_at<=CURRENT_TIMESTAMP'),{'doc':document_id})
        leases=(await conn.execute(text('SELECT * FROM document_feature_leases WHERE document_id=:doc'),{'doc':document_id})).mappings().all()
        own=None
        for row in leases:
            if row['feature_id']==feature_id and row['principal_id']==context.principal_id and row['client_id']==client_id:
                own=row
                if lease_token is not None and row['token']!=lease_token:
                    raise FeatureLeaseConflict('编辑租约已被替换')
            elif related(features,str(feature_id),str(row['feature_id'])):
                raise FeatureLeaseConflict('目标特征或依赖已被其他编辑会话占用')
        if lease_token is not None and own is None:
            raise FeatureLeaseConflict('编辑租约已过期，请重新获取')
        operations=(await conn.execute(text("SELECT arguments FROM cad_operations WHERE document_id=:doc AND status IN ('queued','running')"),{'doc':document_id})).scalars()
        for payload in operations:
            targets=operation_targets(features,payload)
            if targets is None or any(related(features,str(feature_id),target) for target in targets):
                raise FeatureLeaseConflict('目标特征正在执行已提交的变更，请等待任务完成')
        token=own['token'] if own else uuid4()
        row=(await conn.execute(text("""INSERT INTO document_feature_leases(token,tenant_id,document_id,feature_id,principal_id,client_id,revision_id,expires_at)
            VALUES(:token,:tenant,:doc,:feature,:principal,:client,:revision,CURRENT_TIMESTAMP+INTERVAL '90 seconds')
            ON CONFLICT(document_id,feature_id) DO UPDATE SET expires_at=EXCLUDED.expires_at,revision_id=EXCLUDED.revision_id
            RETURNING token,feature_id,expires_at"""),{'token':token,'tenant':context.tenant_id,'doc':document_id,
            'feature':feature_id,'principal':context.principal_id,'client':client_id,'revision':revision_id})).mappings().one()
    return dict(row)


async def release_feature_lease(context,document_id,token):
    from app.services.cloud_documents import authorized_document
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        await authorized_document(conn,context,document_id,lock=True)
        row=(await conn.execute(text('SELECT principal_id FROM document_feature_leases WHERE token=:token AND document_id=:doc'),
                                {'token':token,'doc':document_id})).mappings().one_or_none()
        if row is not None and row['principal_id']!=context.principal_id:
            raise PermissionError('无权释放其他用户的编辑租约')
        await conn.execute(text('DELETE FROM document_feature_leases WHERE token=:token AND document_id=:doc'),{'token':token,'doc':document_id})
