"""Evidence-backed Change Set review and version actions."""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import get_durable_principal
from app.domain.identity import PrincipalContext
from app.models.schemas import (
    ChangeSetActionResponse,
    ChangeSetRequiredNoteRequest,
    ChangeSetReviewRequest,
)
from app.services.change_sets import (
    ArtifactEvidenceRequired,
    ChangeSetStateConflict,
    ValidationRequired,
    accept_change_set,
    commit_change_set,
    reject_change_set,
    request_change_set_modification,
    rollback_change_set,
)
from app.services.event_relay import (
    change_set_workflow_id,
    get_change_set_detail,
    get_task_snapshot,
)
from app.workflows.temporal import confirm_mcad_workflow
from app.evaluation.integration import record_change_set_transition


router = APIRouter(prefix="/api/change-sets", tags=["change-sets"])
logger = logging.getLogger(__name__)


def _evaluation_transition_payload(
    fallback_workflow_id: UUID | None,
    result,
) -> dict[str, str]:
    if isinstance(result, dict):
        workflow_id = result.get("source_workflow_run_id")
    else:
        workflow_id = getattr(result, "source_workflow_run_id", None)
    workflow_id = workflow_id or fallback_workflow_id
    return {"workflow_run_id": str(workflow_id)} if workflow_id else {}


def _operation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="未找到 Change Set")
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail="无权访问该 Change Set")
    if isinstance(exc, (ChangeSetStateConflict,)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (ValidationRequired, ArtifactEvidenceRequired, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc


async def _signal_review(
    principal: PrincipalContext,
    workflow_run_id: UUID | None,
    *,
    accepted: bool,
    note: str,
) -> None:
    if workflow_run_id is None:
        return
    snapshot = await get_task_snapshot(principal, workflow_run_id)
    if snapshot["status"] not in {
        "pending",
        "planning",
        "running",
        "waiting_confirmation",
    }:
        # Compatibility workflows finish with a pending-review Change Set.
        # Their review is a control-plane transition and has no live Temporal
        # history to wake.
        return
    await confirm_mcad_workflow(
        workflow_run_id,
        accepted=accepted,
        note=note,
        workflow_kind=snapshot["kind"],
    )


@router.get("/{change_set_id}")
async def change_set_detail(
    change_set_id: UUID,
    principal: PrincipalContext = Depends(get_durable_principal),
):
    try:
        return await get_change_set_detail(principal, change_set_id)
    except Exception as exc:
        raise _operation_error(exc) from exc


@router.post("/{change_set_id}/accept", response_model=ChangeSetActionResponse)
async def accept_change(
    change_set_id: UUID,
    body: ChangeSetReviewRequest,
    principal: PrincipalContext = Depends(get_durable_principal),
):
    try:
        workflow_id = await change_set_workflow_id(principal, change_set_id)
        result = await accept_change_set(
            tenant_id=principal.tenant_id,
            reviewer_principal_id=principal.principal_id,
            change_set_id=change_set_id,
            review_note=body.note or None,
        )
        record_change_set_transition(
            _evaluation_transition_payload(workflow_id, result),
            "accepted",
            result,
        )
        # Persisted review happens first.  If Temporal is transiently unavailable,
        # retrying this endpoint replays the state transition and re-sends the
        # idempotent workflow signal.
        await _signal_review(
            principal,
            workflow_id,
            accepted=True,
            note=body.note,
        )
        return result
    except Exception as exc:
        try:
            raise _operation_error(exc) from exc
        except HTTPException:
            raise
        except Exception:
            logger.exception("Change Set acceptance signal failed")
            raise HTTPException(
                status_code=503,
                detail="无法确认工作流信号状态；该操作具备幂等性，可安全重试",
            ) from exc


@router.post("/{change_set_id}/reject", response_model=ChangeSetActionResponse)
async def reject_change(
    change_set_id: UUID,
    body: ChangeSetRequiredNoteRequest,
    principal: PrincipalContext = Depends(get_durable_principal),
):
    try:
        workflow_id = await change_set_workflow_id(principal, change_set_id)
        result = await reject_change_set(
            tenant_id=principal.tenant_id,
            reviewer_principal_id=principal.principal_id,
            change_set_id=change_set_id,
            review_note=body.note,
        )
        record_change_set_transition(
            _evaluation_transition_payload(workflow_id, result),
            "rejected",
            result,
        )
        await _signal_review(
            principal,
            workflow_id,
            accepted=False,
            note=body.note,
        )
        return result
    except Exception as exc:
        try:
            raise _operation_error(exc) from exc
        except HTTPException:
            raise
        except Exception:
            logger.exception("Change Set rejection signal failed")
            raise HTTPException(
                status_code=503,
                detail="无法确认工作流信号状态；该操作具备幂等性，可安全重试",
            ) from exc


@router.post(
    "/{change_set_id}/request-change",
    response_model=ChangeSetActionResponse,
)
async def request_change(
    change_set_id: UUID,
    body: ChangeSetRequiredNoteRequest,
    principal: PrincipalContext = Depends(get_durable_principal),
):
    try:
        workflow_id = await change_set_workflow_id(principal, change_set_id)
        result = await request_change_set_modification(
            tenant_id=principal.tenant_id,
            reviewer_principal_id=principal.principal_id,
            change_set_id=change_set_id,
            review_note=body.note,
        )
        record_change_set_transition(
            _evaluation_transition_payload(workflow_id, result),
            "changes_requested",
            result,
        )
        await _signal_review(
            principal,
            workflow_id,
            accepted=False,
            note=body.note,
        )
        return result
    except Exception as exc:
        try:
            raise _operation_error(exc) from exc
        except HTTPException:
            raise
        except Exception:
            logger.exception("Change Set request-change signal failed")
            raise HTTPException(
                status_code=503,
                detail="无法确认工作流信号状态；该操作具备幂等性，可安全重试",
            ) from exc


@router.post("/{change_set_id}/commit", response_model=ChangeSetActionResponse)
async def commit_change(
    change_set_id: UUID,
    principal: PrincipalContext = Depends(get_durable_principal),
):
    try:
        workflow_id = await change_set_workflow_id(principal, change_set_id)
        result = await commit_change_set(
            tenant_id=principal.tenant_id,
            reviewer_principal_id=principal.principal_id,
            change_set_id=change_set_id,
        )
        record_change_set_transition(
            _evaluation_transition_payload(workflow_id, result),
            "committed",
            result,
        )
        return result
    except Exception as exc:
        raise _operation_error(exc) from exc


@router.post("/{change_set_id}/rollback", response_model=ChangeSetActionResponse)
async def rollback_change(
    change_set_id: UUID,
    body: ChangeSetReviewRequest,
    principal: PrincipalContext = Depends(get_durable_principal),
):
    try:
        workflow_id = await change_set_workflow_id(principal, change_set_id)
        result = await rollback_change_set(
            tenant_id=principal.tenant_id,
            reviewer_principal_id=principal.principal_id,
            change_set_id=change_set_id,
            review_note=body.note,
        )
        record_change_set_transition(
            _evaluation_transition_payload(workflow_id, result),
            "rolled_back",
            result,
        )
        return result
    except Exception as exc:
        raise _operation_error(exc) from exc
