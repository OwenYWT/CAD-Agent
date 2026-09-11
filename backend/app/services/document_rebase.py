"""Prove independent parameter writes against two verified semantic checkpoints.

The new operation still executes, validates and enters normal review. This does
not merge candidate files or advance a branch by reusing an old validation.
"""
from uuid import UUID

from sqlalchemy import text

from app.db import tenant_transaction
from app.execution.canonical import canonical_sha256
from app.services.cloud_documents import DocumentConflict,checkpoint
from app.services.feature_leases import ancestors


class ParameterRebaseConflict(DocumentConflict):
    code='parameter_rebase_conflict'


def prove_independent_parameter_edit(before,after,updates):
    old={f['id']:f for f in before['features']}
    new={f['id']:f for f in after['features']}
    parameter_owners={p['id']:f['id'] for f in before['features'] for p in f['parameters']}
    checked=set()
    for update in updates:
        target=parameter_owners.get(update['parameter_id'])
        if target is None:
            raise ParameterRebaseConflict('参数不在原始检查点中')
        checked.update(ancestors(before['features'],target))
    for feature_id in sorted(checked):
        left,right=old.get(feature_id),new.get(feature_id)
        if left is None or right is None:
            raise ParameterRebaseConflict('参数依赖的特征已被删除或替换')
        if any((f.get('sketch') or {}).get('constraint_count',0)>256 for f in (left,right)):
            raise ParameterRebaseConflict('草图约束超过完整记录范围，无法证明可安全重放')
        if (left.get('shape') or right.get('shape')) and (
            not left.get('geometry_sha256') or not right.get('geometry_sha256')
            or left.get('geometry_fingerprint_kind') != 'fcstd-brep.v1'
            or right.get('geometry_fingerprint_kind') != 'fcstd-brep.v1'
        ):
            raise ParameterRebaseConflict('旧检查点缺少完整几何指纹，无法证明此修改可安全重放')
        fields=('kernel_name','type','dependencies','parameters','is_valid','shape','sketch','geometry_sha256','geometry_fingerprint_kind',
                'global_placement','sketch_constraints_sha256','instance')
        if {k:left.get(k) for k in fields}!={k:right.get(k) for k in fields}:
            raise ParameterRebaseConflict(f"特征 {left['label']} 或其依赖已改变，请基于当前版本重新编辑")
    return {'checked_feature_ids':sorted(checked),'parameter_ids':sorted(u['parameter_id'] for u in updates),
            'base_state_sha256':before['parameter_state_sha256'],'target_state_sha256':after['parameter_state_sha256']}


async def resolve_parameter_rebase(context,document_id,doc,body):
    if str(doc['head_revision_id'])==str(body.expected_base_revision_id):
        if doc['state_version']!=body.expected_state_version:
            raise DocumentConflict('文档经历了版本切换，请重新读取后提交')
        return body,None
    if not body.allow_rebase or body.action!='parameters.update' or body.modification is None:
        raise DocumentConflict('文档已更新，请读取当前版本后重新提交')
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        known=await conn.scalar(text("""SELECT sequence FROM document_events WHERE document_id=:doc AND event_type='state_delta'
            AND ((payload->>'revision_id'=:revision AND (payload->>'state_version')::bigint=:version)
              OR (payload->>'base_revision_id'=:revision AND (payload->>'base_state_version')::bigint=:version)) LIMIT 1"""),
            {'doc':document_id,'revision':str(body.expected_base_revision_id),'version':body.expected_state_version})
        if known is None:
            raise ParameterRebaseConflict('原始文档版本与修订记录不一致')
    before=await checkpoint(context,document_id,body.expected_base_revision_id)
    after=await checkpoint(context,document_id,doc['head_revision_id'])
    if body.modification.expected_state_sha256!=before['parameter_state_sha256']:
        raise ParameterRebaseConflict('原始参数状态校验值不一致')
    proof=prove_independent_parameter_edit(before,after,[u.model_dump(mode='json') for u in body.modification.parameter_updates])
    replacement=body.model_copy(update={'expected_base_revision_id':doc['head_revision_id'],'expected_state_version':doc['state_version'],
        'modification':body.modification.model_copy(update={'expected_state_sha256':after['parameter_state_sha256']})})
    evidence={'rebased_from_revision_id':body.expected_base_revision_id,'rebased_from_state_version':body.expected_state_version,
              'rebase_evidence_hash':canonical_sha256(proof)}
    return replacement,evidence
