"""Durable task snapshot, event replay, confirmation, and cancellation APIs."""
from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket
from fastapi.encoders import jsonable_encoder
from starlette.websockets import WebSocketDisconnect

from app.api.auth import (
    get_durable_principal,
    resolve_ws_principal,
    verify_ws_token,
    websocket_auth_token,
)
from app.config import settings
from app.domain.identity import PrincipalContext
from app.domain.projects import Permission
from app.models.schemas import (
    DurableTaskEventPage,
    DurableTaskSnapshot,
    TaskCancellationRequest,
    TaskConfirmationRequest,
)
from app.services.event_relay import (
    ConfirmationProjectionInvalid,
    CursorExpired,
    get_task_snapshot,
    read_task_events,
    workflow_project_id,
)
from app.workflows.temporal import cancel_mcad_workflow, confirm_mcad_workflow


router = APIRouter(prefix="/api/tasks", tags=["durable-tasks"])
logger = logging.getLogger(__name__)
_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}


def _task_snapshot_marker(
    snapshot: dict,
) -> tuple[str, str | None, str | None, str | None, str | None]:
    confirmation = snapshot.get("confirmation")
    change_set = snapshot.get("change_set")
    return (
        str(snapshot.get("status") or ""),
        str(confirmation.get("workflow_run_id"))
        if isinstance(confirmation, dict)
        else None,
        str(confirmation.get("plan_hash"))
        if isinstance(confirmation, dict)
        else None,
        str(change_set.get("id")) if isinstance(change_set, dict) else None,
        str(change_set.get("status")) if isinstance(change_set, dict) else None,
    )


def _confirmation_http_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    )


def _read_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="未找到任务")
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail="无权访问该任务")
    if isinstance(exc, CursorExpired):
        return HTTPException(
            status_code=410,
            detail={
                "code": "event_cursor_expired",
                "message": str(exc),
                "earliest_sequence": exc.earliest_sequence,
                "current_sequence": exc.current_sequence,
            },
        )
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc


@router.get("/{workflow_run_id}/snapshot", response_model=DurableTaskSnapshot)
async def task_snapshot(
    workflow_run_id: UUID,
    principal: PrincipalContext = Depends(get_durable_principal),
):
    try:
        return await get_task_snapshot(principal, workflow_run_id)
    except Exception as exc:
        raise _read_error(exc) from exc


@router.get("/{workflow_run_id}/events", response_model=DurableTaskEventPage)
async def task_events(
    workflow_run_id: UUID,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    principal: PrincipalContext = Depends(get_durable_principal),
):
    try:
        return await read_task_events(
            principal,
            workflow_run_id,
            after_sequence=after_sequence,
            limit=limit,
        )
    except Exception as exc:
        raise _read_error(exc) from exc


@router.post("/{workflow_run_id}/confirmation")
async def confirm_task(
    workflow_run_id: UUID,
    body: TaskConfirmationRequest,
    principal: PrincipalContext = Depends(get_durable_principal),
):
    try:
        await workflow_project_id(
            principal,
            workflow_run_id,
            permission=Permission.REVIEW_CHANGE,
        )
        snapshot = await get_task_snapshot(principal, workflow_run_id)
        if snapshot["status"] != "waiting_confirmation":
            raise _confirmation_http_error(
                409,
                "confirmation_not_waiting",
                "任务当前不在等待确认状态",
            )
        await confirm_mcad_workflow(
            workflow_run_id,
            accepted=body.accepted,
            note=body.note,
            workflow_kind=snapshot["kind"],
        )
        return {
            "workflow_run_id": workflow_run_id,
            "accepted": body.accepted,
            "status": "signal_delivered",
        }
    except HTTPException:
        raise
    except KeyError as exc:
        raise _confirmation_http_error(
            404,
            "task_not_found",
            "未找到任务",
        ) from exc
    except PermissionError as exc:
        raise _confirmation_http_error(
            403,
            "task_forbidden",
            "无权确认该任务",
        ) from exc
    except ConfirmationProjectionInvalid as exc:
        raise _confirmation_http_error(
            500,
            "confirmation_projection_invalid",
            "任务确认信息无效，请联系管理员",
        ) from exc
    except Exception as exc:
        logger.exception("Durable task confirmation failed")
        raise _confirmation_http_error(
            503,
            "confirmation_signal_unavailable",
            "工作流确认服务暂不可用",
            retryable=True,
        ) from exc


@router.post("/{workflow_run_id}/cancel")
async def cancel_task(
    workflow_run_id: UUID,
    body: TaskCancellationRequest,
    principal: PrincipalContext = Depends(get_durable_principal),
):
    try:
        await workflow_project_id(
            principal,
            workflow_run_id,
            permission=Permission.MODIFY_DESIGN,
        )
        await cancel_mcad_workflow(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            workflow_run_id=workflow_run_id,
            reason=body.reason,
        )
        return {
            "workflow_run_id": workflow_run_id,
            "status": "cancellation_requested",
        }
    except Exception as exc:
        try:
            raise _read_error(exc) from exc
        except HTTPException:
            raise
        except Exception:
            logger.exception("Durable task cancellation failed")
            raise HTTPException(
                status_code=503,
                detail="工作流取消服务暂不可用",
            ) from exc


async def durable_task_websocket(
    websocket: WebSocket,
    workflow_run_id: UUID,
) -> None:
    """Replay persisted events and then tail them with bounded backpressure."""
    if not settings.durable_control_plane_enabled:
        await websocket.close(code=1013, reason="Durable control plane is disabled")
        return
    token, auth_protocol = websocket_auth_token(websocket)
    if not await verify_ws_token(token):
        await websocket.close(code=4003, reason="Invalid or missing token")
        return
    principal = await resolve_ws_principal(token)
    if principal is None:
        await websocket.close(code=1013, reason="Durable principal unavailable")
        return
    try:
        cursor = int(websocket.query_params.get("after_sequence", "0"))
        if cursor < 0:
            raise ValueError
        snapshot = await get_task_snapshot(principal, workflow_run_id)
    except (KeyError, PermissionError):
        await websocket.close(code=4003, reason="Task not found or access denied")
        return
    except ValueError:
        await websocket.close(code=4001, reason="Invalid event cursor")
        return

    await websocket.accept(subprotocol=auth_protocol)
    await websocket.send_json({
        "type": "task_snapshot",
        "data": jsonable_encoder(snapshot),
    })
    published_snapshot_marker = _task_snapshot_marker(snapshot)
    try:
        while True:
            try:
                page = await read_task_events(
                    principal,
                    workflow_run_id,
                    after_sequence=cursor,
                    limit=100,
                )
            except CursorExpired as exc:
                await websocket.send_json({
                    "type": "cursor_expired",
                    "data": {
                        "earliest_sequence": exc.earliest_sequence,
                        "current_sequence": exc.current_sequence,
                    },
                })
                await websocket.close(code=4009, reason="Event cursor expired")
                return
            for event in page["events"]:
                # Awaiting each send provides transport backpressure; no
                # per-connection event queue can grow without bound.
                await websocket.send_json({
                    "type": "task_event",
                    "data": jsonable_encoder(event),
                })
                cursor = int(event["sequence"])
            if page["has_more"]:
                continue
            snapshot = await get_task_snapshot(principal, workflow_run_id)
            snapshot_marker = _task_snapshot_marker(snapshot)
            if snapshot_marker != published_snapshot_marker:
                await websocket.send_json({
                    "type": "task_snapshot",
                    "data": jsonable_encoder(snapshot),
                })
                published_snapshot_marker = snapshot_marker
            if (
                snapshot["status"] in _TERMINAL_STATUSES
                and cursor >= int(snapshot["last_event_sequence"])
            ):
                await websocket.send_json({
                    "type": "task_stream_complete",
                    "data": {
                        "workflow_run_id": str(workflow_run_id),
                        "status": snapshot["status"],
                        "last_event_sequence": cursor,
                    },
                })
                await websocket.close(code=1000)
                return
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("Durable task WebSocket failed")
        try:
            await websocket.close(code=1011, reason="Task event stream failed")
        except Exception:
            pass
