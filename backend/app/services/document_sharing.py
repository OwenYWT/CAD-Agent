"""Explicit workspace membership; knowing a tenant/document ID grants nothing."""
import hashlib
import json
import secrets
from uuid import UUID, uuid4

from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.identity import PrincipalContext, PrincipalKind, user_principal
from app.domain.projects import Permission
from app.services.cloud_documents import authorized_document


async def workspace_principal(context: PrincipalContext, tenant_id: UUID) -> PrincipalContext:
    if tenant_id == context.tenant_id:
        return context
    if context.kind != PrincipalKind.USER or not context.external_subject.startswith("user:"):
        raise PermissionError("只有登录用户可以进入共享工作区")
    target = user_principal(context.external_subject.removeprefix("user:"), tenant_id=tenant_id)
    async with tenant_transaction(tenant_id, target.principal_id) as conn:
        member = await conn.scalar(text("""SELECT p.id FROM principals p
            JOIN tenant_memberships m ON m.tenant_id=p.tenant_id AND m.principal_id=p.id
            JOIN tenants t ON t.id=p.tenant_id
            WHERE p.id=:id AND p.external_subject=:subject AND p.status='active' AND t.status='active'"""),
            {"id": target.principal_id, "subject": target.external_subject})
        if member is None:
            raise PermissionError("尚未获得此工作区授权")
    return target


async def create_review_invite(context: PrincipalContext, document_id: UUID, *, role='viewer') -> dict:
    if role not in {'viewer','editor'}:
        raise ValueError('邀请仅支持审阅者或编辑者')
    token = secrets.token_urlsafe(32)
    async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
        await authorized_document(conn, context, document_id, Permission.MANAGE_MEMBERS)
        expires = await conn.scalar(text("""INSERT INTO document_review_invites(id, tenant_id, document_id, token_hash, created_by, role)
            VALUES(:id,:tenant,:doc,:hash,:principal,:role) RETURNING expires_at"""),
            {"id": uuid4(), "tenant": context.tenant_id, "doc": document_id,
             "hash": hashlib.sha256(token.encode()).hexdigest(), "principal": context.principal_id, "role":role})
    return {"token": token, "tenant_id": str(context.tenant_id), "document_id": str(document_id), "expires_at": expires, "role":role}


async def accept_review_invite(context: PrincipalContext, document_id: UUID, tenant_id: UUID, token: str):
    if context.kind != PrincipalKind.USER or not context.external_subject.startswith("user:"):
        raise PermissionError("请登录后接受邀请")
    target = user_principal(context.external_subject.removeprefix("user:"), tenant_id=tenant_id)
    async with tenant_transaction(tenant_id, target.principal_id) as conn:
        invite = (await conn.execute(text("""SELECT i.id, i.used_by, i.role, d.project_id FROM document_review_invites i
            JOIN cloud_documents d ON d.id=i.document_id
            WHERE i.document_id=:doc AND i.token_hash=:hash AND i.expires_at>CURRENT_TIMESTAMP FOR UPDATE OF i"""),
            {"doc": document_id, "hash": hashlib.sha256(token.encode()).hexdigest()})).mappings().one_or_none()
        if invite is None or invite["used_by"] not in {None, target.principal_id}:
            raise PermissionError("邀请已失效或已被使用")
        if invite["used_by"] == target.principal_id:
            # Replay may acknowledge an existing grant, never restore a revoked
            # membership. A fresh invitation is required to grant access again.
            await authorized_document(conn, target, document_id)
            role = await conn.scalar(text('SELECT role FROM project_memberships WHERE project_id=:project AND principal_id=:id'),
                                     {'project':invite['project_id'],'id':target.principal_id})
            return {"document_id":str(document_id),"tenant_id":str(tenant_id),"role":role}
        # Do not call ensure_principal: that bootstrap grants tenant ownership.
        await conn.execute(text("""INSERT INTO principals(id, tenant_id, kind, external_subject, display_name, status)
            VALUES(:id,:tenant,'user',:subject,'受邀项目成员','active') ON CONFLICT(id) DO NOTHING"""),
            {"id": target.principal_id, "tenant": tenant_id, "subject": target.external_subject})
        await conn.execute(text("""INSERT INTO tenant_memberships(tenant_id, principal_id, role)
            VALUES(:tenant,:id,'member') ON CONFLICT DO NOTHING"""), {"tenant": tenant_id, "id": target.principal_id})
        await conn.execute(text("""INSERT INTO project_memberships(tenant_id, project_id, principal_id, role)
            VALUES(:tenant,:project,:id,:role) ON CONFLICT(tenant_id,project_id,principal_id) DO UPDATE
            SET role=CASE WHEN project_memberships.role='viewer' AND EXCLUDED.role='editor' THEN 'editor' ELSE project_memberships.role END"""),
            {"tenant": tenant_id, "project": invite["project_id"], "id": target.principal_id, "role":invite['role']})
        await authorized_document(conn, target, document_id)
        await conn.execute(text("UPDATE document_review_invites SET used_by=:id, used_at=COALESCE(used_at,CURRENT_TIMESTAMP) WHERE id=:invite"),
            {"id": target.principal_id, "invite": invite["id"]})
        role = await conn.scalar(text('SELECT role FROM project_memberships WHERE project_id=:project AND principal_id=:id'),
                                 {'project':invite['project_id'],'id':target.principal_id})
    return {"document_id": str(document_id), "tenant_id": str(tenant_id), "role": role}


async def project_members(context,document_id):
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        doc=await authorized_document(conn,context,document_id,Permission.MANAGE_MEMBERS)
        return [dict(r) for r in (await conn.execute(text("""SELECT m.principal_id,m.role,p.display_name
            FROM project_memberships m JOIN principals p ON p.id=m.principal_id
            WHERE m.project_id=:project ORDER BY m.role,p.display_name,m.principal_id"""),{'project':doc['project_id']})).mappings()]


async def change_project_member(context,document_id,target,*,expected_role,role=None):
    from app.services.cloud_documents import DocumentConflict
    if role not in {None,'viewer','editor'} or expected_role not in {'viewer','editor'}:
        raise ValueError('仅可管理审阅者和编辑者')
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        doc=await authorized_document(conn,context,document_id,Permission.MANAGE_MEMBERS)
        await conn.execute(text('SELECT id FROM projects WHERE id=:id FOR UPDATE'),{'id':doc['project_id']})
        documents=(await conn.execute(text('SELECT id FROM cloud_documents WHERE project_id=:project ORDER BY id FOR UPDATE'),{'project':doc['project_id']})).scalars().all()
        current=await conn.scalar(text('SELECT role FROM project_memberships WHERE project_id=:project AND principal_id=:id FOR UPDATE'),
                                  {'project':doc['project_id'],'id':target})
        if current not in {'viewer','editor'} or target==context.principal_id:
            raise PermissionError('不能通过此入口修改项目所有者、管理员或自己')
        if current!=expected_role:
            raise DocumentConflict('成员权限已改变，请刷新列表后重试')
        if role==current:
            return {'principal_id':str(target),'role':current}
        if role is None:
            await conn.execute(text('DELETE FROM project_memberships WHERE project_id=:project AND principal_id=:id'),{'project':doc['project_id'],'id':target})
        else:
            await conn.execute(text('UPDATE project_memberships SET role=:role WHERE project_id=:project AND principal_id=:id'),
                               {'project':doc['project_id'],'id':target,'role':role})
        for document in documents:
            await conn.execute(text('DELETE FROM document_feature_leases WHERE document_id=:doc AND principal_id=:id'),{'doc':document,'id':target})
            seq=await conn.scalar(text('UPDATE cloud_documents SET event_sequence=event_sequence+1 WHERE id=:doc RETURNING event_sequence'),{'doc':document})
            await conn.execute(text("""INSERT INTO document_events(tenant_id,document_id,sequence,event_type,payload)
                VALUES(:tenant,:doc,:seq,'permissions.changed',CAST(:payload AS jsonb))"""),
                {'tenant':context.tenant_id,'doc':document,'seq':seq,
                 'payload':json.dumps({'principal_id':str(target),'previous_role':current,'role':role})})
    return {'principal_id':str(target),'role':role}
