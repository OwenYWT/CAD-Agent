import asyncio
import logging
import re
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect

from app.agent import run_store
from app.agent.conversation import ConversationContext
from app.api.auth import verify_ws_token, get_ws_user_id, rate_limiter
from app.api.error_messages import public_generation_error
from app.models.schemas import DurableRequestIdentity
from app.config import settings
from app.storage import history
from app.services.durable_submission import (
    ensure_workspace_identity,
    submit_durable_workflow,
)
from app.services.event_relay import get_task_snapshot, workflow_project_id
from app.domain.projects import Permission
from app.workflows.temporal import (
    cancel_mcad_workflow,
    confirm_mcad_workflow,
)

logger = logging.getLogger(__name__)

from collections import OrderedDict

_MAX_SESSIONS = 500
sessions: "OrderedDict[str, dict[str, ConversationContext]]" = OrderedDict()

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9\-]{1,128}$")


def _get_context(session_id: str, panel_id: str) -> ConversationContext:
    """Bounded UI context cache; never owns execution or product state."""
    if session_id not in sessions:
        sessions[session_id] = {}
        while len(sessions) > _MAX_SESSIONS:
            sessions.popitem(last=False)
    else:
        sessions.move_to_end(session_id)
    panel_map = sessions[session_id]
    if panel_id not in panel_map:
        panel_map[panel_id] = ConversationContext(
            session_id=f"{session_id}/{panel_id}"
        )
    return panel_map[panel_id]


def _durable_identity_payload(data: dict) -> dict:
    """Validate and normalize the durable write identity at the WS boundary."""
    identity = DurableRequestIdentity.model_validate(data)
    return identity.model_dump(mode="json", exclude_none=True)


async def _legacy_resume_input(
    run_id: str,
    *,
    session_id: str,
    panel_id: str,
) -> tuple[str, list[str], str]:
    """Read a legacy recovery record without mutating its process-local state."""
    run = await run_store.get_run(run_id)
    if (
        run is None
        or run.get("session_id") != session_id
        or run.get("panel_id") != panel_id
    ):
        raise ValueError("未找到当前面板可继续的旧任务")
    steps = await run_store.list_steps(run_id)
    resume_step = next(
        (
            step
            for step in reversed(steps)
            if step.get("step_type") == "resume_available"
        ),
        None,
    )
    if resume_step is None or resume_step.get("status") != "blocked":
        raise ValueError("当前旧任务没有可继续的步骤")
    output = resume_step.get("output") or {}
    if output.get("next_step") != "execute_cad_code":
        raise ValueError("当前只支持继续执行 MCAD 代码")
    resume_input = output.get("resume_input") or {}
    code = str(resume_input.get("code") or "")
    if not code:
        raise ValueError("续跑输入缺少完整 MCAD 代码")
    output_formats = list(
        resume_input.get("output_formats") or ["step", "stl"]
    )
    objective = str(
        resume_input.get("user_prompt")
        or run.get("user_prompt")
        or "继续执行旧 MCAD 任务"
    )
    return code, output_formats, objective


async def websocket_endpoint(websocket: WebSocket, session_id: str):
    # Validate session_id format
    if not _SESSION_ID_RE.match(session_id):
        await websocket.close(code=4001, reason="Invalid session_id format")
        return

    # Auth check
    token = websocket.query_params.get("token")
    if not await verify_ws_token(token):
        await websocket.close(code=4003, reason="Invalid or missing token")
        return
    user_id = await get_ws_user_id(token)
    from app.domain.identity import (
        api_key_principal,
        local_anonymous_principal,
        user_principal,
    )
    from app.principal_context import bind_principal
    from app.repositories.identity import reconcile_principal

    if user_id:
        principal_context = user_principal(user_id)
    elif token:
        principal_context = api_key_principal(token)
    else:
        principal_context = local_anonymous_principal()
    if settings.durable_control_plane_enabled:
        principal_context = await reconcile_principal(principal_context)
        bind_principal(principal_context)

    # Ownership check: a logged-in user must not attach to a session_id that another
    # user already owns (otherwise they could write panels/messages into it). A brand
    # new session_id, a NULL-owner session, and local-dev anonymous mode (user_id is
    # None) are all allowed; session_writable_by_user encodes exactly that.
    if not await history.session_writable_by_user(session_id, user_id):
        await websocket.close(code=4003, reason="Session belongs to another user")
        return

    await websocket.accept()

    send_lock = asyncio.Lock()

    async def send_json(message: dict) -> None:
        async with send_lock:
            await websocket.send_json(message)

    async def submit_cutover_message(
        data: dict,
        *,
        msg_type: str,
        panel_id: str,
        durable_identity: dict,
    ) -> None:
        if principal_context is None:
            raise RuntimeError("durable principal context is unavailable")
        if msg_type == "user_message":
            text = str(data.get("text") or "")
            capability = data.get("capability", "auto")
            if not text or len(text) > 10000:
                raise ValueError("提示词为空或超过长度限制")
            if capability not in {"auto", "cad", "dxf"}:
                raise ValueError(
                    "该能力需要结构化输入或已有产物，请使用 "
                    "/api/capability-actions"
                )
        elif msg_type == "modify_part":
            part_name = str(data.get("part_name") or "")
            instruction = str(data.get("instruction") or "")
            existing_code = str(data.get("code") or "")
            if (
                not part_name
                or not instruction
                or len(instruction) > 10000
                or not existing_code
                or len(existing_code) > 50000
            ):
                raise ValueError(
                    "零件名、修改指令或当前 MCAD 代码无效"
                )
        elif msg_type == "execute_code":
            submitted_code = str(data.get("code") or "")
            if not submitted_code or len(submitted_code) > 50000:
                raise ValueError("代码为空或超过长度限制")

        if not durable_identity.get("project_id"):
            if msg_type != "user_message":
                raise ValueError(
                    "project_id, branch_id and expected_base_revision_id "
                    "are required after the first project prompt"
                )
            workspace = await ensure_workspace_identity(
                principal_context,
                session_id=session_id,
                panel_id=panel_id,
                title=str(data.get("text") or "")[:80],
                user_id=user_id,
            )
            project_id = workspace.project_id
            branch_id = workspace.branch_id
            expected_base_revision_id = workspace.head_revision_id
        else:
            project_id = UUID(str(durable_identity["project_id"]))
            branch_id = UUID(str(durable_identity["branch_id"]))
            expected_base_revision_id = UUID(
                str(durable_identity["expected_base_revision_id"])
            )

        if msg_type == "user_message":
            current_workflow_id = data.get("workflow_run_id")
            if current_workflow_id:
                workflow_run_id = UUID(str(current_workflow_id))
                snapshot = await get_task_snapshot(
                    principal_context,
                    workflow_run_id,
                )
                if (
                    snapshot["status"] == "waiting_confirmation"
                    and snapshot.get("change_set") is None
                ):
                    await workflow_project_id(
                        principal_context,
                        workflow_run_id,
                        permission=Permission.REVIEW_CHANGE,
                    )
                    request_payload = snapshot["request_payload"]
                    if (
                        UUID(str(snapshot["project_id"])) != project_id
                        or UUID(str(request_payload["branch_id"]))
                        != branch_id
                        or UUID(
                            str(
                                request_payload[
                                    "expected_base_revision_id"
                                ]
                            )
                        )
                        != expected_base_revision_id
                    ):
                        raise PermissionError(
                            "任务身份与当前项目上下文不一致"
                        )
                    await confirm_mcad_workflow(
                        workflow_run_id,
                        accepted=True,
                        note=text,
                        workflow_kind=snapshot["kind"],
                    )
                    await send_json({
                        "type": "task_submitted",
                        "data": {
                            "workflow_run_id": str(workflow_run_id),
                            "project_id": str(project_id),
                            "branch_id": str(branch_id),
                            "expected_base_revision_id": str(
                                expected_base_revision_id
                            ),
                            "panel_id": panel_id,
                            "status": "running",
                        },
                    })
                    return
            operation = "generate"
            objective = text
            if data.get("capability", "auto") == "dxf":
                objective = (
                    "只生成 1:1 的二维 DXF 图纸，不生成三维模型。"
                    "请将下述需求规划为 profile_2d，并输出 DXF：\n"
                    + text
                )
            code = None
            output_formats = (
                ["dxf"]
                if data.get("capability", "auto") == "dxf"
                else ["step", "stl"]
            )
            profile = data.get("manufacturing_profile")
        elif msg_type == "modify_part":
            operation = "modify"
            objective = (
                f"修改零件 {data.get('part_name', '')}: "
                f"{data.get('instruction', '')}"
            )
            code = existing_code
            output_formats = ["step", "stl"]
            profile = None
        elif msg_type == "execute_code":
            operation = "execute"
            objective = "执行用户提交的 MCAD 代码"
            code = str(data.get("code") or "")
            output_formats = ["step", "stl"]
            profile = None
        else:
            raise ValueError(f"unsupported cutover message: {msg_type}")

        submission = await submit_durable_workflow(
            principal_context,
            project_id=project_id,
            branch_id=branch_id,
            expected_base_revision_id=expected_base_revision_id,
            idempotency_key=str(durable_identity["idempotency_key"]),
            operation=operation,
            objective=objective,
            output_formats=output_formats,
            code=code,
            manufacturing_profile=profile,
        )
        await send_json({
            "type": "task_submitted",
            "data": {
                "workflow_run_id": str(submission.workflow_run_id),
                "project_id": str(project_id),
                "branch_id": str(branch_id),
                "expected_base_revision_id": str(
                    expected_base_revision_id
                ),
                "panel_id": panel_id,
                "status": "pending",
            },
        })

    async def submit_cutover_resume(
        data: dict,
        *,
        panel_id: str,
        durable_identity: dict,
    ) -> None:
        if principal_context is None:
            raise RuntimeError("durable principal context is unavailable")
        run_id = str(data.get("run_id") or "")
        if not run_id:
            raise ValueError("缺少要继续的任务 ID")
        code, output_formats, objective = await _legacy_resume_input(
            run_id,
            session_id=session_id,
            panel_id=panel_id,
        )
        project_id = UUID(str(durable_identity["project_id"]))
        branch_id = UUID(str(durable_identity["branch_id"]))
        expected_base_revision_id = UUID(
            str(durable_identity["expected_base_revision_id"])
        )
        submission = await submit_durable_workflow(
            principal_context,
            project_id=project_id,
            branch_id=branch_id,
            expected_base_revision_id=expected_base_revision_id,
            idempotency_key=str(durable_identity["idempotency_key"]),
            operation="execute",
            objective=objective,
            output_formats=output_formats,
            code=code,
        )
        await send_json({
            "type": "task_submitted",
            "data": {
                "workflow_run_id": str(submission.workflow_run_id),
                "project_id": str(project_id),
                "branch_id": str(branch_id),
                "expected_base_revision_id": str(
                    expected_base_revision_id
                ),
                "panel_id": panel_id,
                "status": "pending",
            },
        })

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            panel_id = data.get("panel_id", "default")

            if msg_type in {
                "user_message",
                "modify_part",
                "execute_code",
                "restore_task",
                "cancel",
                "resume_run",
            }:
                if not await history.panel_writable_by_session(panel_id, session_id, user_id):
                    await websocket.close(code=4003, reason="Panel belongs to another session")
                    return

            if msg_type in {
                "user_message",
                "modify_part",
                "execute_code",
                "resume_run",
            }:
                try:
                    if (
                        msg_type == "user_message"
                        and not data.get("project_id")
                    ):
                        idempotency_key = str(
                            data.get("idempotency_key") or ""
                        ).strip()
                        if not idempotency_key or len(idempotency_key) > 500:
                            raise ValueError(
                                "idempotency_key is required for the first "
                                "durable project prompt"
                            )
                        durable_identity = {
                            "idempotency_key": idempotency_key,
                        }
                    else:
                        durable_identity = _durable_identity_payload(data)
                except Exception as exc:
                    await send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": {
                                "type": "ValidationError",
                                "message": str(exc),
                            },
                            "panel_id": panel_id,
                        },
                    })
                    continue
            else:
                durable_identity = {}

            # Rate limit per WebSocket message
            try:
                await rate_limiter.check(websocket, token)
            except Exception:
                await send_json({
                    "type": "generation_result",
                    "data": {
                        "success": False,
                        "error": {"type": "RateLimitError", "message": "请求过于频繁，请稍后再试"},
                        "panel_id": panel_id,
                    },
                })
                continue

            if msg_type in {"user_message", "modify_part", "execute_code"}:
                try:
                    await submit_cutover_message(
                        data,
                        msg_type=msg_type,
                        panel_id=panel_id,
                        durable_identity=durable_identity,
                    )
                except Exception as exc:
                    logger.error(
                        "Durable WebSocket submission failed for %s/%s: %s",
                        session_id,
                        panel_id,
                        type(exc).__name__,
                    )
                    error = public_generation_error(exc)
                    if error["type"] == "ValueError":
                        error["type"] = "ValidationError"
                    await send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": error,
                            "panel_id": panel_id,
                        },
                    })
                continue

            if msg_type == "resume_run":
                try:
                    await submit_cutover_resume(
                        data,
                        panel_id=panel_id,
                        durable_identity=durable_identity,
                    )
                except Exception as exc:
                    logger.error(
                        "Durable legacy resume failed for %s/%s: %s",
                        session_id,
                        panel_id,
                        type(exc).__name__,
                    )
                    await send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": public_generation_error(exc),
                            "panel_id": panel_id,
                        },
                    })
                continue

            if msg_type == "cancel":
                workflow_run_id = data.get("workflow_run_id")
                if not workflow_run_id or principal_context is None:
                    await send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": {
                                "type": "TaskNotFound",
                                "message": "没有可取消的持久任务。",
                            },
                            "panel_id": panel_id,
                        },
                    })
                    continue
                try:
                    await cancel_mcad_workflow(
                        tenant_id=principal_context.tenant_id,
                        principal_id=principal_context.principal_id,
                        workflow_run_id=UUID(str(workflow_run_id)),
                        reason="用户取消",
                    )
                    await send_json({
                        "type": "task_status",
                        "data": {
                            "task_id": str(workflow_run_id),
                            "panel_id": panel_id,
                            "status": "cancellation_requested",
                        },
                    })
                except Exception as exc:
                    await send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": public_generation_error(exc),
                            "panel_id": panel_id,
                        },
                    })
                continue

            if msg_type == "restore_task":
                await send_json({
                    "type": "task_status",
                    "data": {
                        "task_id": data.get("workflow_run_id"),
                        "panel_id": panel_id,
                        "status": "durable_subscription",
                    },
                })
                continue

            if msg_type == "restore_context":
                # Durable task state is restored through snapshot/event APIs.
                # Client-supplied source is not accepted as product state.
                continue

            await send_json({
                "type": "generation_result",
                "data": {
                    "success": False,
                    "error": {
                        "type": "ValidationError",
                        "message": "不支持的消息类型。",
                    },
                    "panel_id": panel_id,
                },
            })
            continue

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {session_id}")
    except Exception as e:
        logger.error(
            "WebSocket error for %s with %s",
            session_id,
            type(e).__name__,
        )
        try:
            await websocket.close()
        except Exception:
            pass
