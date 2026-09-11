"""Outbound-only local daemon pairing and fenced file-delivery acknowledgements."""
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import secrets
from types import SimpleNamespace
from uuid import UUID,uuid4

from sqlalchemy import text
from app.db import tenant_transaction
from app.domain.projects import Permission
from app.object_store import get_object
from app.services.cloud_documents import DocumentConflict,authorized_document
from app.services.document_releases import release_result


def _hash(value):return hashlib.sha256(value.encode()).hexdigest()


def _credential(value):
    try:
        tenant,bridge,secret=value.split('.')
        if len(secret)!=43:raise ValueError()
        return UUID(tenant),UUID(bridge),secret
    except (ValueError,AttributeError) as exc:raise PermissionError('本地连接凭据无效') from exc


async def create_pairing(context,document_id,label):
    secret=secrets.token_urlsafe(32);bridge_id=uuid4()
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        doc=await authorized_document(conn,context,document_id,Permission.MANAGE_MEMBERS)
        expires=await conn.scalar(text('''INSERT INTO local_bridges(id,tenant_id,project_id,document_id,created_by,label,pair_hash)
            VALUES(:id,:tenant,:project,:doc,:principal,:label,:hash) RETURNING pair_expires_at'''),
            {'id':bridge_id,'tenant':context.tenant_id,'project':doc['project_id'],'doc':document_id,
             'principal':context.principal_id,'label':label.strip(),'hash':_hash(secret)})
    return {'bridge_id':str(bridge_id),'pairing_code':f'{context.tenant_id}.{bridge_id}.{secret}','expires_at':expires}


async def claim_pairing(body):
    tenant,bridge_id,secret=_credential(body.pairing_code);token=secrets.token_urlsafe(32)
    async with tenant_transaction(tenant) as conn:
        bridge=(await conn.execute(text('''SELECT * FROM local_bridges WHERE id=:id AND revoked_at IS NULL
            AND paired_at IS NULL AND pair_expires_at>CURRENT_TIMESTAMP FOR UPDATE'''),{'id':bridge_id})).mappings().one_or_none()
        if bridge is None or not hmac.compare_digest(bridge['pair_hash'] or '',_hash(secret)):
            raise PermissionError('配对码无效、已使用或已过期')
        await authorized_document(conn,SimpleNamespace(tenant_id=tenant,principal_id=bridge['created_by']),bridge['document_id'],Permission.MANAGE_MEMBERS)
        await conn.execute(text('''UPDATE local_bridges SET paired_at=CURRENT_TIMESTAMP,last_seen_at=CURRENT_TIMESTAMP,
            pair_hash=NULL,token_hash=:hash,client_info=CAST(:info AS jsonb) WHERE id=:id'''),
            {'id':bridge_id,'hash':_hash(token),'info':body.client_info.model_dump_json()})
    return {'bridge_id':str(bridge_id),'tenant_id':str(tenant),'project_id':str(bridge['project_id']),
        'token':f'{tenant}.{bridge_id}.{token}'}


@asynccontextmanager
async def bridge_transaction(credential):
    tenant,bridge_id,secret=_credential(credential)
    async with tenant_transaction(tenant) as conn:
        bridge=(await conn.execute(text('''SELECT * FROM local_bridges WHERE id=:id AND paired_at IS NOT NULL
            AND revoked_at IS NULL FOR UPDATE'''),{'id':bridge_id})).mappings().one_or_none()
        if bridge is None or not hmac.compare_digest(bridge['token_hash'] or '',_hash(secret)):
            raise PermissionError('本地连接凭据已失效')
        # Revoking the pairing administrator also stops the daemon immediately.
        await authorized_document(conn,SimpleNamespace(tenant_id=tenant,principal_id=bridge['created_by']),bridge['document_id'],Permission.MANAGE_MEMBERS)
        await conn.execute(text('UPDATE local_bridges SET last_seen_at=CURRENT_TIMESTAMP WHERE id=:id'),{'id':bridge_id})
        yield conn,bridge


async def list_bridges(context,document_id):
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        doc=await authorized_document(conn,context,document_id)
        bridges=[dict(r) for r in (await conn.execute(text('''SELECT id,label,paired_at,last_seen_at,revoked_at,client_info,
            (last_seen_at>CURRENT_TIMESTAMP-INTERVAL '45 seconds' AND revoked_at IS NULL) AS online
            FROM local_bridges WHERE project_id=:project ORDER BY created_at DESC LIMIT 100'''),{'project':doc['project_id']})).mappings()]
        deliveries=[dict(r) for r in (await conn.execute(text('''SELECT id,bridge_id,release_id,status,attempts,error_message,receipt,created_at,finished_at
            FROM bridge_deliveries WHERE document_id=:doc ORDER BY created_at DESC LIMIT 50'''),{'doc':document_id})).mappings()]
    return {'bridges':bridges,'deliveries':deliveries}


async def revoke_bridge(context,document_id,bridge_id):
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        doc=await authorized_document(conn,context,document_id,Permission.MANAGE_MEMBERS)
        bridge=await conn.scalar(text('SELECT id FROM local_bridges WHERE id=:id AND project_id=:project FOR UPDATE'),{'id':bridge_id,'project':doc['project_id']})
        if not bridge:raise KeyError(bridge_id)
        await conn.execute(text('UPDATE local_bridges SET revoked_at=COALESCE(revoked_at,CURRENT_TIMESTAMP),token_hash=NULL,pair_hash=NULL WHERE id=:id'),{'id':bridge_id})
        await conn.execute(text("UPDATE bridge_deliveries SET status='cancelled',finished_at=CURRENT_TIMESTAMP,error_message='本地连接已撤销' WHERE bridge_id=:id AND status IN ('queued','leased')"),{'id':bridge_id})


async def queue_delivery(context,document_id,release_id,bridge_id):
    result=await release_result(context,document_id,release_id)
    ref=result['artifacts']['engineering_bundle'];manifest_ref=result['artifacts']['release_manifest']
    async with tenant_transaction(context.tenant_id,context.principal_id) as conn:
        doc=await authorized_document(conn,context,document_id,Permission.EXPORT_ARTIFACT)
        bridge=await conn.scalar(text('''SELECT id FROM local_bridges WHERE id=:id AND project_id=:project
            AND paired_at IS NOT NULL AND revoked_at IS NULL FOR UPDATE'''),{'id':bridge_id,'project':doc['project_id']})
        if bridge is None:raise ValueError('请选择本项目已配对且未撤销的本地连接')
        previous=await conn.scalar(text("SELECT id FROM bridge_deliveries WHERE bridge_id=:bridge AND release_id=:release AND status NOT IN ('failed','cancelled')"),{'bridge':bridge_id,'release':release_id})
        if previous:return {'delivery_id':str(previous),'replayed':True}
        if await conn.scalar(text("SELECT count(*) FROM bridge_deliveries WHERE bridge_id=:id AND status IN ('queued','leased')"),{'id':bridge_id})>=100:
            raise DocumentConflict('此本地连接已有 100 个待交付任务')
        manifest_row=(await conn.execute(text('SELECT object_key FROM artifacts WHERE id=:id'),{'id':UUID(manifest_ref['artifact_id'])})).mappings().one()
        raw=await get_object(manifest_row['object_key'])
        if len(raw)!=manifest_ref['size_bytes'] or hashlib.sha256(raw).hexdigest()!=manifest_ref['sha256']:
            raise ValueError('发布清单完整性校验失败')
        manifest=json.loads(raw)
        if manifest['source']['document_id']!=str(document_id) or manifest['source']['revision_id']!=result['source_revision_id']:
            raise ValueError('发布清单来源不一致')
        delivery=uuid4()
        await conn.execute(text('''INSERT INTO bridge_deliveries(id,tenant_id,project_id,document_id,bridge_id,release_id,requested_by,
            archive_artifact_id,archive_sha256,archive_size_bytes,manifest_sha256,manifest)
            VALUES(:id,:tenant,:project,:doc,:bridge,:release,:principal,:artifact,:sha,:size,:manifest_sha,CAST(:manifest AS jsonb))'''),
            {'id':delivery,'tenant':context.tenant_id,'project':doc['project_id'],'doc':document_id,'bridge':bridge_id,'release':release_id,
             'principal':context.principal_id,'artifact':UUID(ref['artifact_id']),'sha':ref['sha256'],'size':ref['size_bytes'],
             'manifest_sha':manifest_ref['sha256'],'manifest':raw.decode()})
    return {'delivery_id':str(delivery),'replayed':False}


async def claim_delivery(credential):
    async with bridge_transaction(credential) as (conn,bridge):
        rows=(await conn.execute(text('''SELECT * FROM bridge_deliveries WHERE bridge_id=:bridge
            AND (status='queued' OR status='leased' AND lease_expires_at<CURRENT_TIMESTAMP)
            ORDER BY created_at,id LIMIT 100 FOR UPDATE SKIP LOCKED'''),{'bridge':bridge['id']})).mappings().all()
        for row in rows:
            try:await authorized_document(conn,SimpleNamespace(tenant_id=bridge['tenant_id'],principal_id=row['requested_by']),row['document_id'],Permission.EXPORT_ARTIFACT)
            except (PermissionError,KeyError):
                await conn.execute(text("UPDATE bridge_deliveries SET status='cancelled',finished_at=CURRENT_TIMESTAMP,error_message='导出授权已撤销' WHERE id=:id"),{'id':row['id']});continue
            if row['attempts']>=5:
                await conn.execute(text("UPDATE bridge_deliveries SET status='failed',finished_at=CURRENT_TIMESTAMP,error_message='本地连接多次中断，请核对本地目录后重新交付' WHERE id=:id"),{'id':row['id']});continue
            lease=uuid4()
            expires=await conn.scalar(text('''UPDATE bridge_deliveries SET status='leased',lease_token=:token,
                lease_expires_at=CURRENT_TIMESTAMP+INTERVAL '5 minutes',attempts=attempts+1 WHERE id=:id RETURNING lease_expires_at'''),{'id':row['id'],'token':lease})
            return {'delivery':{'delivery_id':str(row['id']),'release_id':str(row['release_id']),'project_id':str(row['project_id']),
                'document_id':str(row['document_id']),'lease_token':str(lease),'lease_expires_at':expires,
                'archive_sha256':row['archive_sha256'],'archive_size_bytes':row['archive_size_bytes'],'manifest_sha256':row['manifest_sha256'],
                'archive_url':f"/api/local-bridge/deliveries/{row['id']}/archive"}}
    return {'delivery':None}


async def _leased(conn,bridge,delivery_id,lease_token):
    row=(await conn.execute(text('''SELECT *,lease_expires_at>CURRENT_TIMESTAMP AS valid FROM bridge_deliveries
        WHERE id=:id AND bridge_id=:bridge FOR UPDATE'''),{'id':delivery_id,'bridge':bridge['id']})).mappings().one_or_none()
    if row is None:raise KeyError(delivery_id)
    if row['lease_token']!=lease_token or row['status'] not in {'leased','delivered'} or (row['status']=='leased' and not row['valid']):
        raise DocumentConflict('本地交付租约已失效')
    await authorized_document(conn,SimpleNamespace(tenant_id=bridge['tenant_id'],principal_id=row['requested_by']),row['document_id'],Permission.EXPORT_ARTIFACT)
    return row


async def delivery_archive(credential,delivery_id,lease_token):
    async with bridge_transaction(credential) as (conn,bridge):
        row=await _leased(conn,bridge,delivery_id,lease_token)
        artifact=(await conn.execute(text('SELECT * FROM artifacts WHERE id=:id'),{'id':row['archive_artifact_id']})).mappings().one()
        raw=await get_object(artifact['object_key'])
        if len(raw)!=row['archive_size_bytes'] or hashlib.sha256(raw).hexdigest()!=row['archive_sha256']:
            raise ValueError('发布归档完整性校验失败')
        return raw


async def renew_delivery(credential,delivery_id,lease_token):
    async with bridge_transaction(credential) as (conn,bridge):
        row=await _leased(conn,bridge,delivery_id,lease_token)
        if row['status']=='leased':
            await conn.execute(text("UPDATE bridge_deliveries SET lease_expires_at=CURRENT_TIMESTAMP+INTERVAL '5 minutes' WHERE id=:id"),{'id':delivery_id})
    return {'status':row['status']}


async def acknowledge_delivery(credential,delivery_id,receipt):
    async with bridge_transaction(credential) as (conn,bridge):
        row=await _leased(conn,bridge,delivery_id,receipt.lease_token)
        expected={f['path']:f['sha256'] for f in row['manifest']['files']};expected['manifest.json']=row['manifest_sha256']
        if receipt.archive_sha256!=row['archive_sha256'] or receipt.manifest_sha256!=row['manifest_sha256'] or receipt.verified_files!=expected or receipt.file_count!=len(expected) or receipt.total_bytes!=sum(f['size_bytes'] for f in row['manifest']['files']):
            raise ValueError('本地回执未验证全部发布文件')
        value=receipt.model_dump(mode='json',exclude={'lease_token'})
        if row['status']=='delivered':
            if row['receipt']!=value:raise DocumentConflict('此交付已有不同回执')
        else:
            await conn.execute(text("UPDATE bridge_deliveries SET status='delivered',receipt=CAST(:receipt AS jsonb),finished_at=CURRENT_TIMESTAMP WHERE id=:id"),{'id':delivery_id,'receipt':json.dumps(value)})
    return {'status':'delivered','delivery_id':str(delivery_id)}


async def fail_delivery(credential,delivery_id,body):
    async with bridge_transaction(credential) as (conn,bridge):
        row=await _leased(conn,bridge,delivery_id,body.lease_token)
        if row['status']!='leased':raise DocumentConflict('交付已经完成')
        await conn.execute(text("UPDATE bridge_deliveries SET status='failed',error_message=:message,finished_at=CURRENT_TIMESTAMP WHERE id=:id"),
            {'id':delivery_id,'message':body.message})
    return {'status':'failed'}
