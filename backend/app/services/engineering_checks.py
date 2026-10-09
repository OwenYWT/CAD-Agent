"""Read durable engineering results with project authorization and artifact integrity."""
from __future__ import annotations

import hashlib
import json
from uuid import UUID

from sqlalchemy import text

from app.db import tenant_transaction
from app.models.schemas import DesignAnalysisResponse
from app.object_store import get_object
from app.services.event_relay import get_task_snapshot, workflow_project_id
from app.domain.projects import Permission


async def execution_rule_configuration(connection, *, tenant_id: UUID,
        principal_id: UUID, workflow_id: UUID, process: str | None,
        supplied: dict | None) -> dict:
    """Use admitted rules; capture legacy inputs once without rewriting requests."""
    from app.dfm.configuration import configuration_rules, capture_configuration
    from app.repositories.runs import append_workflow_event

    row = (await connection.execute(text("""
        SELECT requested_by_principal_id, kind, request_payload FROM workflow_runs
        WHERE tenant_id=:tenant AND id=:workflow FOR UPDATE
    """), {"tenant": tenant_id, "workflow": workflow_id})).mappings().one()
    if row["requested_by_principal_id"] != principal_id or row["kind"] != "mcad.check":
        raise ValueError("Engineering configuration ownership mismatch")
    configuration = row["request_payload"].get("rule_configuration")
    if supplied != configuration:
        raise ValueError("Engineering configuration differs from the durable request")
    if configuration is None:
        configuration = await connection.scalar(text("""
            SELECT payload->'rule_configuration' FROM task_events
            WHERE tenant_id=:tenant AND workflow_run_id=:workflow
              AND event_type='engineering.configuration_captured'
            ORDER BY sequence LIMIT 1
        """), {"tenant": tenant_id, "workflow": workflow_id})
        if configuration is None:
            configuration = await capture_configuration(connection, tenant_id=tenant_id,
                principal_id=principal_id, process=process, origin="legacy_activity")
            await append_workflow_event(connection, tenant_id=tenant_id,
                workflow_id=workflow_id, event_type="engineering.configuration_captured",
                payload={"rule_configuration": configuration})
    configuration_rules(configuration, process=process, tenant_id=tenant_id,
                        principal_id=principal_id)
    return configuration


async def read_engineering_check(principal, workflow_id: UUID) -> dict:
    snapshot = await get_task_snapshot(principal, workflow_id)
    if snapshot['kind'] != 'mcad.check':
        raise ValueError('该任务不是工程检查')
    result = {'workflow_run_id': str(workflow_id), 'task_status': snapshot['status'],
              'error': snapshot.get('error_message'), 'analysis': None}
    if snapshot['status'] != 'succeeded':
        return result
    async with tenant_transaction(principal.tenant_id, principal.principal_id) as connection:
        row = (await connection.execute(text('''
            SELECT a.object_key, a.size_bytes, a.sha256, a.revision_id,
                   w.request_payload
            FROM artifacts a JOIN workflow_runs w
              ON w.tenant_id=a.tenant_id AND w.id=a.workflow_run_id
            WHERE a.tenant_id=:tenant AND a.workflow_run_id=:workflow
              AND a.artifact_kind='dfm_report'
            ORDER BY a.created_at DESC, a.id DESC LIMIT 1
        '''), {'tenant': principal.tenant_id, 'workflow': workflow_id})).mappings().one_or_none()
    if row is None:
        raise ValueError('检查已结束，但缺少有效报告')
    data = await get_object(row['object_key'])
    if len(data) != row['size_bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
        raise ValueError('检查报告完整性校验失败')
    report = json.loads(data)
    source = str(row['request_payload']['source_revision_id'])
    if (str(row['revision_id']) != source or report.get('source_revision_id') != source
            or report.get('workflow_run_id') != str(workflow_id)):
        raise ValueError('检查报告来源版本不匹配')
    analysis = DesignAnalysisResponse.model_validate(report['analysis'])
    configuration = row['request_payload'].get('rule_configuration')
    if configuration is not None:
        from app.dfm.configuration import configuration_rules
        configuration_rules(configuration, process=row['request_payload'].get('process'))
        if report.get('rule_configuration') != configuration or analysis.rule_configuration != configuration:
            raise ValueError('检查报告规则配置与受理时快照不匹配')
    analysis.source_revision_id = source
    result.update(analysis=analysis.model_dump(mode='json'), source_revision_id=source,
                  report_sha256=row['sha256'])
    return result


async def authorize_check_source(principal, workflow_id: UUID):
    await workflow_project_id(principal, workflow_id, permission=Permission.VIEW_PROJECT)
