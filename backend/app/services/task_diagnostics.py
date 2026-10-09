"""Owned failure observations; diagnostic bytes never become a checkpoint."""
from __future__ import annotations
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID
from sqlalchemy import text
from app.db import tenant_transaction
from app.domain.projects import Permission
from app.object_store import get_object
from app.services.event_relay import get_task_snapshot, workflow_project_id
from app.workflows.constraint_evidence import evidence_key


async def failure_diagnostic(principal, workflow_id: UUID, *, describe_requirements=None) -> dict:
    await workflow_project_id(principal, workflow_id, permission=Permission.VIEW_PROJECT)
    task = await get_task_snapshot(principal, workflow_id)
    async with tenant_transaction(principal.tenant_id, principal.principal_id) as conn:
        event = (await conn.execute(text('''SELECT payload FROM task_events
            WHERE tenant_id=:tenant AND workflow_run_id=:workflow
              AND event_type='agent.freecad.constraint_diagnostic'
            ORDER BY sequence DESC LIMIT 1'''), {'tenant': principal.tenant_id, 'workflow': workflow_id})).scalar_one_or_none()
        if event is None:
            raise KeyError('此任务未保存草图失败现场')
        source_row = (await conn.execute(text('''SELECT source_hash,source_code FROM agent_generated_sources
            WHERE tenant_id=:tenant AND workflow_run_id=:workflow AND id=:source'''),
            {'tenant': principal.tenant_id, 'workflow': workflow_id, 'source': UUID(event['source_id'])})).mappings().one_or_none()
        source = source_row['source_hash'] if source_row else None
        if source != event['source_hash']:
            raise ValueError('诊断对应的执行来源不匹配')
        base_state_version = await conn.scalar(text('SELECT base_state_version FROM cad_operations WHERE id=:id'), {'id': workflow_id})
        requirements = await conn.scalar(text('''SELECT payload FROM task_events
            WHERE tenant_id=:tenant AND workflow_run_id=:workflow AND event_type='agent.requirements.completed'
            ORDER BY sequence LIMIT 1'''),{'tenant':principal.tenant_id,'workflow':workflow_id})
    request = SimpleNamespace(tenant_id=principal.tenant_id, workflow_run_id=workflow_id)
    raw = await get_object(evidence_key(request, event['sha256']))
    if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != event['sha256']:
        raise ValueError('失败现场完整性校验失败')
    envelope = json.loads(raw)
    if (envelope.get('schema_version') != 'constraint-evidence.v1'
        or envelope.get('workflow_run_id') != str(workflow_id)
        or envelope.get('source_id') != event['source_id']
        or envelope.get('source_hash') != source
        or envelope.get('execution_attempt_id') != event['execution_attempt_id']
        or envelope.get('snapshot', {}).get('valid_checkpoint') is not False):
        raise ValueError('失败现场归属不匹配')
    protection={'requirement_bindings':[],'protection_note':'来源未知或不支持的关系继续保护。'}
    if source_row and requirements and describe_requirements:
        protection=describe_requirements(source_row['source_code'],envelope['snapshot'],requirements)
    return {**envelope, 'sha256': event['sha256'], 'task_status': task['status'],
            **protection,
            'base_revision_id': task['request_payload'].get('expected_base_revision_id'),
            'base_state_version': base_state_version,
            'document_id': task['request_payload'].get('branch_id')}
