"""Public boundary for starting and controlling durable MCAD workflows."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from temporalio.client import WorkflowHandle
from temporalio.service import RPCError

from app.db import tenant_transaction
from app.models.workflow_requests import (
    OperationContextV1,
    FreeCADRevisionRestoreV1,
    McadOutputRequest,
    McadExecutionRequest,
    McadSourcePreparationRequest,
    McadWorkflowRequest,
    McadAgentWorkflowV2Request,
    McadCheckRequest
)
from app.models.native_modification import (FreeCADParameterUpdateV1, FreeCADNativeEditV1, FreeCADStructuredModificationV1)
from app.services.workflow_dispatch import persist_dispatch, start_dispatch, acknowledge_dispatch
from app.config import settings
from app.services.workflow_admission import create_document_workflow
from app.services.run_state import (
    IllegalTransition,
    create_workflow,
    request_workflow_cancellation,
)
from app.temporal_client import (
    get_temporal_client,
    temporal_agent_v2_worker_readiness,
)

logger = logging.getLogger(__name__)


async def _dispatch_after_commit(dispatch: dict) -> WorkflowHandle | None:
    """A committed request stays accepted during a transport interruption.

    The actual workflow handle is absent until delivery; the DB status remains
    queued and the dispatcher recovers the same immutable Temporal ID.
    """
    async def deliver():
        client = await get_temporal_client()
        return await start_dispatch(client, dispatch)

    try:
        handle = await asyncio.wait_for(deliver(), timeout=5)
    except (RPCError, OSError, TimeoutError, RuntimeError) as exc:
        logger.warning("Accepted workflow %s awaits dispatch (%s)",
                       dispatch["workflow_id"], type(exc).__name__)
        return None
    try:
        await acknowledge_dispatch(dispatch)
    except DBAPIError as exc:
        logger.warning("Delivered workflow %s awaits dispatch acknowledgement (%s)",
                       dispatch["workflow_id"], type(exc).__name__)
    return handle


















def temporal_workflow_id(workflow_run_id: UUID | str) -> str:
    return f"mcad-workflow-{workflow_run_id}"


def temporal_check_workflow_id(workflow_run_id: UUID | str) -> str:
    return f"mcad-check-workflow-{workflow_run_id}"


def temporal_agent_v2_workflow_id(workflow_run_id: UUID | str) -> str:
    return f"mcad-agent-v2-workflow-{workflow_run_id}"


def mcad_workflow_request_payload(
    *,
    branch_id: UUID,
    expected_base_revision_id: UUID,
    objective: str,
    primary: McadExecutionRequest | None,
    preparation: McadSourcePreparationRequest | None,
    followup: McadExecutionRequest | None,
    require_confirmation: bool,
    confirmation_timeout_seconds: int,
    commit_after_confirmation: bool,
) -> dict[str, Any]:
    """Canonical immutable payload used for idempotency comparisons."""
    return {
        "objective": objective,
        "branch_id": str(branch_id),
        "expected_base_revision_id": str(expected_base_revision_id),
        "primary": primary.model_dump(mode="json") if primary else None,
        "preparation": (
            preparation.model_dump(mode="json") if preparation else None
        ),
        "followup": followup.model_dump(mode="json") if followup else None,
        "require_confirmation": require_confirmation,
        "confirmation_timeout_seconds": confirmation_timeout_seconds,
        "commit_after_confirmation": commit_after_confirmation,
    }


def mcad_agent_v2_request_payload(
    *,
    branch_id: UUID,
    expected_base_revision_id: UUID,
    operation: Literal["generate", "modify"],
    objective: str,
    existing_code: str | None,
    manufacturing_profile: dict[str, Any] | None,
    output_formats: tuple[str, ...],
    confirmation_timeout_seconds: int,
    modeling_backend: Literal["auto", "cadquery", "freecad"] = "cadquery",
    operation_context: OperationContextV1 | None = None,
    structured_modification: FreeCADStructuredModificationV1 | None = None,
    revision_restore: FreeCADRevisionRestoreV1 | None = None,
    expected_state_version: int | None = None,
) -> dict[str, Any]:
    """Canonical immutable V2 payload used by WorkflowRun idempotency."""
    payload = {
        "branch_id": str(branch_id),
        "expected_base_revision_id": str(expected_base_revision_id),
        "operation": operation,
        "objective": objective,
        "existing_code": existing_code,
        "manufacturing_profile": manufacturing_profile,
        "output_formats": list(output_formats),
        "confirmation_timeout_seconds": confirmation_timeout_seconds,
    }
    if modeling_backend != "cadquery":
        payload["modeling_backend"] = modeling_backend
    if operation_context is not None:
        payload["operation_context"] = operation_context.model_dump(mode="json")
    if structured_modification is not None:
        payload["structured_modification"] = structured_modification.model_dump(
            mode="json"
        )
    if revision_restore is not None:
        payload["revision_restore"] = revision_restore.model_dump(mode="json")
    if expected_state_version is not None:
        payload["expected_state_version"] = expected_state_version
    return payload


async def start_mcad_agent_v2_workflow(
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID,
    branch_id: UUID,
    expected_base_revision_id: UUID,
    kind: str,
    idempotency_key: str,
    operation: Literal["generate", "modify"],
    objective: str,
    existing_code: str | None = None,
    manufacturing_profile: dict[str, Any] | None = None,
    output_formats: tuple[str, ...] = ("step", "stl"),
    confirmation_timeout_seconds: int = 3600,
    require_worker_ready: bool = True,
    modeling_backend: Literal["auto", "cadquery", "freecad"] = "cadquery",
    operation_context: OperationContextV1 | None = None,
    structured_modification: FreeCADStructuredModificationV1 | None = None,
    revision_restore: FreeCADRevisionRestoreV1 | None = None,
    expected_state_version: int | None = None,
) -> tuple[UUID, WorkflowHandle | None]:
    """Fail closed on V2 readiness, persist once, then start V2 idempotently."""
    objective = objective.strip()
    kind = kind.strip()
    idempotency_key = idempotency_key.strip()
    request_payload = mcad_agent_v2_request_payload(
        branch_id=branch_id,
        expected_base_revision_id=expected_base_revision_id,
        operation=operation,
        objective=objective,
        existing_code=existing_code,
        manufacturing_profile=manufacturing_profile,
        output_formats=output_formats,
        confirmation_timeout_seconds=confirmation_timeout_seconds,
        modeling_backend=modeling_backend,
        operation_context=operation_context,
        structured_modification=structured_modification,
        revision_restore=revision_restore,
        expected_state_version=expected_state_version,
    )
    # New work is accepted only when V2 has pollers. An already-persisted
    # idempotent submission may re-enter this boundary without readiness so a
    # crash between the DB commit and Temporal start can still be repaired.
    if require_worker_ready:
        await temporal_agent_v2_worker_readiness()
    async with tenant_transaction(tenant_id, principal_id) as connection:
        created = await create_document_workflow(
            connection,
            tenant_id=tenant_id,
            project_id=project_id,
            requested_by_principal_id=principal_id,
            kind=kind,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )
        durable_request = McadAgentWorkflowV2Request(
            document_queue=True,
            expected_state_version=expected_state_version,
            workflow_run_id=created.workflow_id,
            tenant_id=tenant_id,
            project_id=project_id,
            principal_id=principal_id,
            branch_id=branch_id,
            expected_base_revision_id=expected_base_revision_id,
            operation=operation,
            modeling_backend=modeling_backend,
            operation_context=operation_context,
            structured_modification=structured_modification,
            revision_restore=revision_restore,
            objective=objective,
            existing_code=existing_code,
            manufacturing_profile=manufacturing_profile,
            output_formats=output_formats,
            confirmation_timeout_seconds=confirmation_timeout_seconds,
        )
        dispatch = await persist_dispatch(connection, tenant_id=tenant_id, principal_id=principal_id,
            workflow_id=created.workflow_id, workflow_type="McadAgentWorkflowV2",
            temporal_id=temporal_agent_v2_workflow_id(created.workflow_id), task_queue=settings.temporal_agent_v2_task_queue,
            payload=durable_request.temporal_payload())
    handle = await _dispatch_after_commit(dispatch)
    return created.workflow_id, handle

async def start_mcad_workflow(
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID,
    branch_id: UUID,
    expected_base_revision_id: UUID,
    kind: str,
    idempotency_key: str,
    objective: str,
    primary: McadExecutionRequest | None = None,
    preparation: McadSourcePreparationRequest | None = None,
    followup: McadExecutionRequest | None = None,
    require_confirmation: bool = True,
    confirmation_timeout_seconds: int = 3600,
    commit_after_confirmation: bool = True,
    expected_state_version: int | None = None,
) -> tuple[UUID, WorkflowHandle | None]:
    """Persist WorkflowRun first, then idempotently start its Temporal history."""
    objective = objective.strip()
    kind = kind.strip()
    idempotency_key = idempotency_key.strip()
    if not objective or len(objective) > 4000:
        raise ValueError("objective must contain 1 to 4000 characters")
    if not kind or len(kind) > 200:
        raise ValueError("kind must contain 1 to 200 characters")
    if not idempotency_key or len(idempotency_key) > 500:
        raise ValueError(
            "idempotency_key must contain 1 to 500 characters"
        )
    if not 1 <= confirmation_timeout_seconds <= 604800:
        raise ValueError(
            "confirmation_timeout_seconds must be between 1 and 604800"
        )
    if commit_after_confirmation and not require_confirmation:
        raise ValueError(
            "commit_after_confirmation requires explicit confirmation"
        )
    if (primary is None) == (preparation is None):
        raise ValueError(
            "exactly one of primary or preparation must be provided"
        )
    request_payload = mcad_workflow_request_payload(
        branch_id=branch_id,
        expected_base_revision_id=expected_base_revision_id,
        objective=objective,
        primary=primary,
        preparation=preparation,
        followup=followup,
        require_confirmation=require_confirmation,
        confirmation_timeout_seconds=confirmation_timeout_seconds,
        commit_after_confirmation=commit_after_confirmation,
    )
    if expected_state_version is not None:
        request_payload["expected_state_version"] = expected_state_version
    async with tenant_transaction(tenant_id, principal_id) as connection:
        created = await create_document_workflow(
            connection,
            tenant_id=tenant_id,
            project_id=project_id,
            requested_by_principal_id=principal_id,
            kind=kind,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )
        durable_request = McadWorkflowRequest(
            document_queue=True,
            expected_state_version=expected_state_version,
            workflow_run_id=created.workflow_id,
            tenant_id=tenant_id,
            project_id=project_id,
            principal_id=principal_id,
            branch_id=branch_id,
            expected_base_revision_id=expected_base_revision_id,
            objective=objective,
            primary=primary,
            preparation=preparation,
            followup=followup,
            require_confirmation=require_confirmation,
            confirmation_timeout_seconds=confirmation_timeout_seconds,
            commit_after_confirmation=commit_after_confirmation,
        )
        dispatch = await persist_dispatch(connection, tenant_id=tenant_id, principal_id=principal_id,
            workflow_id=created.workflow_id, workflow_type="McadDurableWorkflow",
            temporal_id=temporal_workflow_id(created.workflow_id), task_queue=settings.temporal_task_queue,
            payload=durable_request.temporal_payload())
    handle = await _dispatch_after_commit(dispatch)
    return created.workflow_id, handle

async def start_mcad_check_workflow(
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID,
    source_workflow_run_id: UUID,
    source_revision_id: UUID,
    idempotency_key: str,
    code: str = "",
    description: str = "",
    process: str | None = None,
    material: str | None = None,
    timeout_seconds: int = 120,
    configuration_scoped_idempotency: bool = False,
) -> tuple[UUID, WorkflowHandle | None]:
    """Persist and idempotently start a read-only engineering-check run."""
    idempotency_key = idempotency_key.strip()
    if not idempotency_key or len(idempotency_key) > 500:
        raise ValueError(
            "idempotency_key must contain 1 to 500 characters"
        )
    request_payload = {
        "source_workflow_run_id": str(source_workflow_run_id),
        "source_revision_id": str(source_revision_id),
        "code": code,
        "description": description,
        "process": process,
        "material": material,
        "timeout_seconds": timeout_seconds,
    }
    from app.dfm.configuration import capture_configuration
    from app.services.run_state import IdempotencyConflict

    async with tenant_transaction(tenant_id, principal_id) as connection:
        # Serialize same-key admissions so an acknowledgement-loss retry can
        # reuse its original configuration even if settings changed meanwhile.
        await connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"check:{tenant_id}:{idempotency_key}"})
        configuration = None
        if configuration_scoped_idempotency:
            configuration = await capture_configuration(connection, tenant_id=tenant_id,
                principal_id=principal_id, process=process)
            idempotency_key = f"{idempotency_key}:{configuration['sha256']}"
        previous = (await connection.execute(text("""
            SELECT project_id, requested_by_principal_id, kind, request_payload
            FROM workflow_runs WHERE tenant_id=:tenant AND idempotency_key=:key
        """), {"tenant": tenant_id, "key": idempotency_key})).mappings().one_or_none()
        if previous is not None:
            saved = previous["request_payload"]
            if (previous["project_id"] != project_id
                    or previous["requested_by_principal_id"] != principal_id
                    or previous["kind"] != "mcad.check"
                    or any(saved.get(key) != value for key, value in request_payload.items())):
                raise IdempotencyConflict("Engineering check key was reused with different inputs")
            request_payload = saved
        else:
            if configuration is None:
                configuration = await capture_configuration(connection, tenant_id=tenant_id,
                    principal_id=principal_id, process=process)
            request_payload["rule_configuration"] = configuration
        created = await create_workflow(
            connection,
            tenant_id=tenant_id,
            project_id=project_id,
            requested_by_principal_id=principal_id,
            kind="mcad.check",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )
        durable_request = McadCheckRequest(
            workflow_run_id=created.workflow_id,
            source_workflow_run_id=source_workflow_run_id,
            source_revision_id=source_revision_id,
            tenant_id=tenant_id,
            project_id=project_id,
            principal_id=principal_id,
            code=code,
            description=description,
            process=process,
            material=material,
            rule_configuration=request_payload.get("rule_configuration"),
            timeout_seconds=timeout_seconds,
        )
        dispatch = await persist_dispatch(connection, tenant_id=tenant_id, principal_id=principal_id,
            workflow_id=created.workflow_id, workflow_type="McadCheckWorkflow",
            temporal_id=temporal_check_workflow_id(created.workflow_id), task_queue=settings.temporal_task_queue,
            payload=durable_request.temporal_payload())
    handle = await _dispatch_after_commit(dispatch)
    return created.workflow_id, handle

async def confirm_mcad_workflow(
    workflow_run_id: UUID,
    *,
    accepted: bool,
    note: str = "",
    workflow_kind: str | None = None,
) -> None:
    client = await get_temporal_client()
    workflow_id = (
        temporal_agent_v2_workflow_id(workflow_run_id)
        if workflow_kind and workflow_kind.startswith("mcad.agent.v2")
        else temporal_workflow_id(workflow_run_id)
    )
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal(
        "confirmation",
        {"accepted": accepted, "note": note[:4000]},
    )


async def cancel_mcad_workflow(
    *,
    tenant_id: UUID,
    principal_id: UUID,
    workflow_run_id: UUID,
    reason: str = "用户取消",
) -> None:
    """Persist cancellation intent before notifying Temporal."""
    async with tenant_transaction(tenant_id, principal_id) as connection:
        workflow_kind = await connection.scalar(
            text("SELECT kind FROM workflow_runs WHERE id=:id"),
            {"id": workflow_run_id},
        )
        if workflow_kind is None:
            raise KeyError(workflow_run_id)
        try:
            await request_workflow_cancellation(connection, workflow_run_id)
        except IllegalTransition:
            # A terminal WorkflowRun treats cancellation as an idempotent no-op.
            status = await connection.scalar(
                text("SELECT status FROM workflow_runs WHERE id=:id"),
                {"id": workflow_run_id},
            )
            if status in {"succeeded", "failed", "cancelled", "timed_out"}:
                return
            else:
                raise
        status = await connection.scalar(
            text("SELECT status FROM workflow_runs WHERE id=:id"),
            {"id": workflow_run_id},
        )
        if status in {"succeeded", "failed", "cancelled", "timed_out"}:
            return
    client = await get_temporal_client()
    workflow_id = (
        temporal_check_workflow_id(workflow_run_id)
        if workflow_kind in {"mcad.check", "mcad.engineering"}
        else temporal_agent_v2_workflow_id(workflow_run_id)
        if str(workflow_kind).startswith("mcad.agent.v2")
        else temporal_workflow_id(workflow_run_id)
    )
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal("cancel_requested", reason[:4000])
