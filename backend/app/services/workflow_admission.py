"""Document admission composed inside the caller-owned database transaction.

The generic run state machine does not infer document mutations from payloads.
Call this boundary for model/branch operations; checks and scene reads use the
run state machine directly. Dispatch intent must be persisted before commit.
"""
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy import text

from app.domain.runs import WorkflowCreated
from app.execution.canonical import canonical_sha256
from app.repositories.runs import append_workflow_event
from app.services.cloud_documents import DocumentConflict, enqueue_operation
from app.services.run_state import create_workflow
from app.services.provider_budgets import admit as admit_provider_budget


async def native_import_is_queued(connection: AsyncConnection, workflow_id: UUID) -> bool:
    params = {"id": workflow_id}
    status = await connection.scalar(text("SELECT status FROM workflow_runs WHERE id=:id FOR UPDATE"), params)
    # Read after acquiring the lock so a concurrent admission's commit is visible.
    queued = await connection.scalar(text("SELECT EXISTS (SELECT 1 FROM cad_operations WHERE id=:id)"), params)
    if not queued and status != "pending":
        raise DocumentConflict("导入请求已结束，无法继续上传")
    return queued


async def create_document_workflow(
    connection: AsyncConnection, *, tenant_id: UUID, project_id: UUID,
    requested_by_principal_id: UUID, kind: str, idempotency_key: str,
    request_payload: dict[str, Any], import_uploaded: bool = False,
) -> WorkflowCreated:
    created = await create_workflow(
        connection, tenant_id=tenant_id, project_id=project_id,
        requested_by_principal_id=requested_by_principal_id, kind=kind,
        idempotency_key=idempotency_key, request_payload=request_payload,
    )
    if created.replayed:
        if (request_payload.get("operation_context") or {}).get("rule") != "explicit_native_import":
            return created
        # Concurrent matching uploads may finish together. Promote their
        # reservation to one queued operation in the outbox transaction.
        if await native_import_is_queued(connection, created.workflow_id):
            return created
        if not import_uploaded:
            raise ValueError("导入文件尚未上传，请使用原始文件继续导入")
    await admit_provider_budget(connection, tenant_id=tenant_id, workflow_id=created.workflow_id)
    await enqueue_operation(
        connection, workflow_id=created.workflow_id, tenant_id=tenant_id,
        principal_id=requested_by_principal_id, payload=request_payload,
        idempotency_key=idempotency_key, request_hash=canonical_sha256(request_payload),
    )
    context = request_payload.get('operation_context') or {}
    if context.get('source_candidate_revision_id'):
        source = (await connection.execute(text('''SELECT c.base_revision_id,c.base_state_version,c.status,a.id,a.sha256
            FROM change_sets c JOIN artifacts a ON a.revision_id=c.candidate_revision_id AND a.tenant_id=c.tenant_id
            WHERE c.tenant_id=:tenant AND c.project_id=:project AND c.branch_id=:branch
              AND c.candidate_revision_id=:source AND a.artifact_kind='fcstd'
            FOR UPDATE OF c'''), {'tenant':tenant_id,'project':project_id,
                'branch':UUID(request_payload['branch_id']), 'source':UUID(context['source_candidate_revision_id'])})).mappings().one_or_none()
        if (source is None or source['status'] != 'changes_requested'
            or str(source['base_revision_id']) != request_payload['expected_base_revision_id']
            or source['base_state_version'] != request_payload.get('expected_state_version')
            or str(source['id']) != context['base_source_id'] or source['sha256'] != context['base_source_sha256']):
            raise ValueError('候选续改来源、原始基线或检查点身份不匹配')
    rebase = request_payload.get("operation_context") or {}
    if rebase.get("rebased_from_revision_id"):
        await append_workflow_event(
            connection, tenant_id=tenant_id, workflow_id=created.workflow_id,
            event_type="document.operation_rebased", payload={key: rebase[key] for key in (
                "rebased_from_revision_id", "rebased_from_state_version",
                "rebase_evidence_hash", "base_revision_id",
            )},
        )
    return created
