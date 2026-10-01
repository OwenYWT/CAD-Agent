"""Document admission composed inside the caller-owned database transaction.

The generic run state machine does not infer document mutations from payloads.
Call this boundary for model/branch operations; checks and scene reads use the
run state machine directly. Dispatch intent must be persisted before commit.
"""
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.runs import WorkflowCreated
from app.execution.canonical import canonical_sha256
from app.repositories.runs import append_workflow_event
from app.services.cloud_documents import enqueue_operation
from app.services.run_state import create_workflow


async def create_document_workflow(
    connection: AsyncConnection, *, tenant_id: UUID, project_id: UUID,
    requested_by_principal_id: UUID, kind: str, idempotency_key: str,
    request_payload: dict[str, Any],
) -> WorkflowCreated:
    created = await create_workflow(
        connection, tenant_id=tenant_id, project_id=project_id,
        requested_by_principal_id=requested_by_principal_id, kind=kind,
        idempotency_key=idempotency_key, request_payload=request_payload,
    )
    if created.replayed:
        return created
    await enqueue_operation(
        connection, workflow_id=created.workflow_id, tenant_id=tenant_id,
        principal_id=requested_by_principal_id, payload=request_payload,
        idempotency_key=idempotency_key, request_hash=canonical_sha256(request_payload),
    )
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
