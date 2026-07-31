import asyncio
import hashlib
import logging
import re
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect

from app.agent import run_store
from app.agent.orchestrator import ConversationContext, Orchestrator
from app.agent.recovery import ensure_run_resume_available, recover_running_run, sanitize_incomplete_steps
from app.api.auth import verify_ws_token, get_ws_user_id, rate_limiter
from app.api.error_messages import public_generation_error
from app.models.schemas import DurableRequestIdentity, StepUpdate
from app.config import settings
from app.storage import history, local_runs
from app.storage.file_ownership import claim_request_owner
from app.services.durable_submission import (
    ensure_workspace_identity,
    submit_durable_workflow,
)
from app.services.event_relay import get_task_snapshot, workflow_project_id
from app.domain.projects import Permission
from app.workflows.local import LocalWorkflowOutcome, get_local_workflow_manager
from app.workflows.temporal import (
    cancel_mcad_workflow,
    confirm_mcad_workflow,
)

logger = logging.getLogger(__name__)

from collections import OrderedDict

# sessions[session_id][panel_id] -> ConversationContext
# LRU-bounded: in-memory context is a cache (durable state lives in history.py / sqlite),
# so evicting the least-recently-used session just drops cached context — a reconnect
# can restore it via the restore_context message. Bounds memory under many sessions.
_MAX_SESSIONS = 500
sessions: "OrderedDict[str, dict[str, ConversationContext]]" = OrderedDict()

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9\-]{1,128}$")

# Shared orchestrator singleton
_orchestrator: Orchestrator | None = None


def _workflow_scope(session_id: str, panel_id: str) -> str:
    return f"ws:{session_id}:{panel_id}"


def _get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


def _get_context(session_id: str, panel_id: str) -> ConversationContext:
    if session_id not in sessions:
        sessions[session_id] = {}
        # Evict least-recently-used sessions once over capacity.
        while len(sessions) > _MAX_SESSIONS:
            evicted, _ = sessions.popitem(last=False)
            logger.info(f"Evicted LRU session context: {evicted}")
    else:
        sessions.move_to_end(session_id)  # mark recently used
    panel_map = sessions[session_id]
    if panel_id not in panel_map:
        panel_map[panel_id] = ConversationContext(session_id=f"{session_id}/{panel_id}")
    return panel_map[panel_id]


def _durable_identity_payload(data: dict) -> dict:
    """Validate and normalize the durable write identity at the WS boundary."""
    identity = DurableRequestIdentity.model_validate(data)
    return identity.model_dump(mode="json", exclude_none=True)


def _attach_snapshot_identity(result_data: dict, snapshot: dict) -> None:
    """Attach durable identity only when the backing store proves each value.

    The compatibility SQLite store used by legacy/local deployments predates
    project branches.  Omitting unavailable fields preserves that contract and
    prevents a legacy snapshot ID from masquerading as a durable revision.
    """
    for field in ("project_id", "branch_id", "revision_id"):
        value = snapshot.get(field)
        if value:
            result_data[field] = value
    if snapshot.get("revision_id"):
        result_data["expected_base_revision_id"] = snapshot["revision_id"]


def _run_created_payload(run: dict, panel_id: str) -> dict:
    return {
        "run_id": run["id"],
        "session_id": run["session_id"],
        "panel_id": panel_id,
        "status": run["status"],
        "capability": run.get("capability"),
        "user_prompt": run.get("user_prompt"),
        "created_at": run.get("created_at"),
    }


def _run_payload(run: dict, panel_id: str) -> dict:
    payload = _run_created_payload(run, panel_id)
    payload["completed_at"] = run.get("completed_at")
    payload["updated_at"] = run.get("updated_at")
    payload["result_request_id"] = run.get("result_request_id")
    return payload


def _agent_step_payload(step: StepUpdate, panel_id: str, *, run_id: str | None = None) -> dict:
    payload = step.model_dump()
    return {
        "run_id": run_id,
        "panel_id": panel_id,
        "step_type": step.step,
        "status": step.status or "running",
        "message": step.message,
        "started_at": step.started_at,
        "duration_ms": step.duration_ms,
        "detail": step.detail,
        "legacy_step": payload,
    }


def _artifact_update_payload(result_data: dict, panel_id: str) -> list[dict]:
    request_id = result_data.get("request_id")
    files = result_data.get("files") or {}
    artifacts = []
    for artifact_type, path in files.items():
        artifacts.append({
            "request_id": request_id,
            "panel_id": panel_id,
            "artifact_type": artifact_type,
            "path": path,
        })
    return artifacts


async def _send_artifact_updates(send_json, result_data: dict, panel_id: str) -> None:
    for artifact in _artifact_update_payload(result_data, panel_id):
        await send_json({"type": "artifact_update", "data": artifact})


def _agent_step_payload_from_record(step: dict, panel_id: str) -> dict:
    status = step.get("status") or "running"
    error = step.get("error") or {}
    output = step.get("output") or {}
    message = error.get("message") or output.get("message") or step.get("step_type") or "agent step"
    return {
        "run_id": step.get("run_id"),
        "panel_id": panel_id,
        "step_type": step.get("step_type"),
        "status": "success" if status == "succeeded" else status,
        "message": message,
        "started_at": step.get("started_at"),
        "duration_ms": None,
        "detail": {"source": "history_replay", "error": error or None, "output": output or None},
        "legacy_step": {
            "step": step.get("step_type"),
            "message": message,
            "status": "success" if status == "succeeded" else status,
            "started_at": step.get("started_at"),
            "duration_ms": None,
            "detail": {"source": "history_replay", "error": error or None, "output": output or None},
        },
    }


async def _replay_latest_run(send_json, session_id: str, panel_id: str) -> None:
    run = await run_store.get_latest_run_for_panel(session_id, panel_id)
    if not run:
        run = await run_store.get_latest_resumable_run()
    if not run:
        return
    if run.get("status") == "running":
        await recover_running_run(run)
        run = await run_store.get_run(run["id"]) or run
    elif run.get("status") in {"failed", "blocked"}:
        await ensure_run_resume_available(run["id"])
        run = await run_store.get_run(run["id"]) or run
    await sanitize_incomplete_steps(run["id"])
    run = await run_store.get_latest_resumable_run_for_panel(session_id, panel_id) or await run_store.get_run(run["id"]) or run
    await send_json({"type": "run_created", "data": _run_payload(run, panel_id)})
    for step in await run_store.list_steps(run["id"]):
        await send_json({"type": "agent_step", "data": _agent_step_payload_from_record(step, panel_id)})
    for artifact in await run_store.list_artifacts(run["id"]):
        await send_json({
            "type": "artifact_update",
            "data": {
                "request_id": run.get("result_request_id"),
                "panel_id": panel_id,
                "artifact_type": artifact["artifact_type"],
                "path": artifact["path"],
            },
        })


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
    principal = f"user:{user_id}" if user_id else token
    principal_context = None
    if settings.durable_control_plane_enabled:
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

    orchestrator = (
        None
        if settings.durable_api_cutover_enabled
        else _get_orchestrator()
    )
    workflow_manager = (
        None
        if settings.durable_api_cutover_enabled
        else get_local_workflow_manager()
    )
    send_lock = asyncio.Lock()
    delivery_tasks: set[asyncio.Task] = set()

    async def send_json(message: dict) -> None:
        async with send_lock:
            await websocket.send_json(message)

    async def make_on_step(
        panel_id: str,
        task_ref: dict | None = None,
        *,
        run_id: str | None = None,
    ):
        async def on_step(step: StepUpdate):
            payload = step.model_dump()
            payload["panel_id"] = panel_id
            if task_ref and task_ref.get("id"):
                payload["task_id"] = task_ref["id"]
            await send_json({"type": "step_update", "data": payload})
            await send_json({
                "type": "agent_step",
                "data": _agent_step_payload(step, panel_id, run_id=run_id),
            })
        return on_step

    def launch_result_delivery(
        task_id: str,
        panel_id: str,
        *,
        progress_delivery=None,
    ) -> None:
        if workflow_manager is None:
            raise RuntimeError("legacy workflow manager is disabled")

        async def deliver() -> None:
            try:
                result_data = await workflow_manager.wait(task_id, owner=principal)
                result_data = dict(result_data)
                result_data["panel_id"] = panel_id
                result_data["task_id"] = task_id
                await _send_artifact_updates(send_json, result_data, panel_id)
                await send_json({
                    "type": "generation_result",
                    "data": result_data,
                })
            except asyncio.CancelledError:
                raise
            except Exception:
                # The terminal result is durable before this delivery begins.
                logger.info("Result delivery stopped for workflow %s", task_id)
            finally:
                if progress_delivery is not None:
                    workflow_manager.detach_progress_delivery(
                        task_id,
                        progress_delivery,
                    )

        delivery_task = asyncio.create_task(
            deliver(),
            name=f"ws-result-delivery:{task_id}",
        )
        delivery_tasks.add(delivery_task)
        delivery_task.add_done_callback(delivery_tasks.discard)

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
                        settings.durable_api_cutover_enabled
                        and msg_type == "user_message"
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

            if (
                settings.durable_api_cutover_enabled
                and msg_type
                in {"user_message", "modify_part", "execute_code"}
            ):
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
                    await send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": public_generation_error(exc),
                            "panel_id": panel_id,
                        },
                    })
                continue

            if (
                settings.durable_api_cutover_enabled
                and msg_type == "resume_run"
            ):
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

            if settings.durable_api_cutover_enabled and msg_type == "cancel":
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

            if (
                settings.durable_api_cutover_enabled
                and msg_type == "restore_task"
            ):
                await send_json({
                    "type": "task_status",
                    "data": {
                        "task_id": data.get("workflow_run_id"),
                        "panel_id": panel_id,
                        "status": "durable_subscription",
                    },
                })
                continue

            if (
                msg_type
                in {
                    "user_message",
                    "modify_part",
                    "execute_code",
                    "resume_run",
                    "restore_task",
                }
                and (orchestrator is None or workflow_manager is None)
            ):
                raise RuntimeError(
                    "legacy workflow dependencies are unavailable"
                )

            if msg_type == "user_message":
                text = data.get("text", "")
                capability = data.get("capability", "auto")
                manufacturing_profile = data.get("manufacturing_profile")
                if not text or len(text) > 10000:
                    await send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": {"type": "ValidationError", "message": "提示词为空或超过长度限制"},
                            "panel_id": panel_id,
                        },
                    })
                    continue
                if capability not in {"auto", "cad", "dxf"}:
                    await send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": {
                                "type": "ValidationError",
                                "message": (
                                    "该能力需要结构化输入或已有产物，请使用 /api/capability-actions，"
                                    "不能按普通 CAD 对话执行。"
                                ),
                            },
                            "panel_id": panel_id,
                        },
                    })
                    continue

                effective_text = text
                if capability == "dxf":
                    effective_text = (
                        "只生成 1:1 的二维 DXF 图纸，不生成三维模型。"
                        "请将下述需求规划为 profile_2d，并输出 DXF：\n" + text
                    )

                context = _get_context(session_id, panel_id)
                task_ref: dict[str, str] = {}
                run = await run_store.create_run(
                    session_id,
                    effective_text,
                    panel_id=panel_id,
                    capability=capability,
                )
                on_step = await make_on_step(
                    panel_id,
                    task_ref,
                    run_id=run["id"],
                )
                await send_json({
                    "type": "run_created",
                    "data": _run_created_payload(run, panel_id),
                })

                await history.create_session(session_id, title=text[:80], user_id=user_id)
                await history.create_panel(session_id, panel_id, user_id=user_id)
                await history.save_message(panel_id, "user", text)

                async def run_generation(progress):
                    try:
                        if manufacturing_profile is not None:
                            result = await orchestrator.handle_message(
                                context,
                                effective_text,
                                on_step=progress,
                                manufacturing_profile=manufacturing_profile,
                                run_id=run["id"],
                            )
                        else:
                            result = await orchestrator.handle_message(
                                context,
                                effective_text,
                                on_step=progress,
                                run_id=run["id"],
                            )
                        result_data = result.model_dump(mode="json")
                    except Exception as exc:
                        logger.error(
                            "生成任务 %s/%s 失败，错误类型 %s",
                            session_id,
                            panel_id,
                            type(exc).__name__,
                        )
                        failed_run = await run_store.get_run(run["id"])
                        if failed_run and failed_run.get("status") == "running":
                            await run_store.complete_run(
                                run["id"],
                                status="failed",
                                result_request_id=task_ref.get("id"),
                            )
                        result_data = {
                            "request_id": task_ref["id"],
                            "success": False,
                            "error": public_generation_error(exc),
                        }

                    result_data["panel_id"] = panel_id
                    result_data["task_id"] = task_ref["id"]
                    if result_data.get("success") and result_data.get("code"):
                        snapshot = await history.create_model_snapshot(
                            panel_id, result_data, source="generation", prompt=text
                        )
                        result_data["snapshot_id"] = snapshot["id"]
                        result_data["version"] = snapshot["version"]
                        _attach_snapshot_identity(result_data, snapshot)
                    await claim_request_owner(
                        result_data.get("request_id"),
                        principal,
                        revision_id=result_data.get("snapshot_id"),
                    )

                    if result_data.get("needs_confirmation"):
                        assistant_content = "设计简报需要确认"
                    elif result_data.get("success"):
                        assistant_content = "CAD 模型已生成"
                    else:
                        error_message = (result_data.get("error") or {}).get(
                            "message", "未知错误"
                        )
                        assistant_content = f"生成失败: {error_message}"
                    await history.save_message(
                        panel_id,
                        "assistant",
                        assistant_content,
                        result=result_data,
                    )
                    if result_data.get("success") and result_data.get("code"):
                        params = result_data.get("params")
                        await history.update_panel_code(
                            panel_id,
                            result_data["code"],
                            params,
                        )
                    await history.touch_session(session_id)
                    if not result_data.get("success") and result_data.get(
                        "request_id"
                    ) == task_ref["id"]:
                        return LocalWorkflowOutcome(result_data, state="FAILED")
                    return result_data

                task_id = await workflow_manager.submit(
                    kind="generate",
                    owner=principal,
                    request={
                        "session_id": session_id,
                        "panel_id": panel_id,
                        "text": text,
                        "capability": capability,
                        "manufacturing_profile": manufacturing_profile,
                        **durable_identity,
                    },
                    runner=run_generation,
                    progress_delivery=on_step,
                    scope_key=_workflow_scope(session_id, panel_id),
                )
                task_ref["id"] = task_id
                launch_result_delivery(task_id, panel_id)

            elif msg_type == "modify_part":
                part_name = data.get("part_name", "")
                instruction = data.get("instruction", "")
                if not part_name or not instruction or len(instruction) > 10000:
                    await send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": {"type": "ValidationError", "message": "零件名或修改指令为空"},
                            "panel_id": panel_id,
                        },
                    })
                    continue

                context = _get_context(session_id, panel_id)
                task_ref: dict[str, str] = {}
                on_step = await make_on_step(panel_id, task_ref)

                # Ensure session+panel rows exist before saving a message — otherwise
                # the FK on messages.panel_id rejects the insert. (user_message does this too.)
                await history.create_session(session_id, title="", user_id=user_id)
                await history.create_panel(session_id, panel_id, user_id=user_id)
                await history.save_message(panel_id, "user", f"修改零件 {part_name}: {instruction}")

                async def run_part_modification(progress):
                    try:
                        result = await orchestrator.modify_assembly_part(
                            context, part_name, instruction, on_step=progress
                        )
                        result_data = result.model_dump(mode="json")
                    except Exception as exc:
                        logger.error(
                            "Part modification failed for %s/%s with %s",
                            session_id,
                            panel_id,
                            type(exc).__name__,
                        )
                        result_data = {
                            "request_id": task_ref["id"],
                            "success": False,
                            "error": public_generation_error(exc),
                        }
                    result_data["panel_id"] = panel_id
                    result_data["task_id"] = task_ref["id"]
                    if result_data.get("success") and result_data.get("code"):
                        snapshot = await history.create_model_snapshot(
                            panel_id,
                            result_data,
                            source="modify_part",
                            prompt=f"修改零件 {part_name}: {instruction}",
                        )
                        result_data["snapshot_id"] = snapshot["id"]
                        result_data["version"] = snapshot["version"]
                        _attach_snapshot_identity(result_data, snapshot)
                    await claim_request_owner(
                        result_data.get("request_id"),
                        principal,
                        revision_id=result_data.get("snapshot_id"),
                    )

                    msg_content = (
                        f"零件 {part_name} 已修改"
                        if result_data.get("success")
                        else "零件修改失败: "
                        + (result_data.get("error") or {}).get("message", "未知错误")
                    )
                    await history.save_message(
                        panel_id,
                        "assistant",
                        msg_content,
                        result=result_data,
                    )
                    if result_data.get("success") and result_data.get("code"):
                        await history.update_panel_code(panel_id, result_data["code"])
                    await history.touch_session(session_id)
                    if not result_data.get("success") and result_data.get(
                        "request_id"
                    ) == task_ref["id"]:
                        return LocalWorkflowOutcome(result_data, state="FAILED")
                    return result_data

                task_id = await workflow_manager.submit(
                    kind="modify_part",
                    owner=principal,
                    request={
                        "session_id": session_id,
                        "panel_id": panel_id,
                        "part_name": part_name,
                        "instruction": instruction,
                        **durable_identity,
                    },
                    runner=run_part_modification,
                    progress_delivery=on_step,
                    scope_key=_workflow_scope(session_id, panel_id),
                )
                task_ref["id"] = task_id
                launch_result_delivery(task_id, panel_id)

            elif msg_type == "execute_code":
                code = data.get("code", "")
                if not code or len(code) > 50000:
                    await send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": {"type": "ValidationError", "message": "代码为空或超过长度限制"},
                            "panel_id": panel_id,
                        },
                    })
                    continue
                context = _get_context(session_id, panel_id)
                await history.create_session(session_id, title="", user_id=user_id)
                await history.create_panel(session_id, panel_id, user_id=user_id)
                task_ref: dict[str, str] = {}

                async def run_code_execution(_progress):
                    try:
                        response = await orchestrator.execute_code(code)
                        result_data = response.model_dump(mode="json")
                    except Exception as exc:
                        logger.error(
                            "Code execution failed for %s/%s with %s",
                            session_id,
                            panel_id,
                            type(exc).__name__,
                        )
                        result_data = {
                            "request_id": task_ref["id"],
                            "success": False,
                            "error": public_generation_error(exc),
                        }

                    if result_data.get("success") and result_data.get("code"):
                        context.current_code = result_data["code"]
                        await history.update_panel_code(
                            panel_id,
                            result_data["code"],
                            result_data.get("params"),
                        )

                    result_data["panel_id"] = panel_id
                    result_data["task_id"] = task_ref["id"]
                    if result_data.get("success") and result_data.get("code"):
                        snapshot = await history.create_model_snapshot(
                            panel_id,
                            result_data,
                            source="execute_code",
                            prompt="manual code execution",
                        )
                        result_data["snapshot_id"] = snapshot["id"]
                        result_data["version"] = snapshot["version"]
                        _attach_snapshot_identity(result_data, snapshot)
                    await claim_request_owner(
                        result_data.get("request_id"),
                        principal,
                        revision_id=result_data.get("snapshot_id"),
                    )
                    assistant_content = (
                        "参数修改已执行"
                        if result_data.get("success")
                        else "参数修改失败: "
                        + (result_data.get("error") or {}).get("message", "未知错误")
                    )
                    await history.save_message(
                        panel_id,
                        "assistant",
                        assistant_content,
                        result=result_data,
                    )
                    await history.touch_session(session_id)
                    if not result_data.get("success") and result_data.get(
                        "request_id"
                    ) == task_ref["id"]:
                        return LocalWorkflowOutcome(result_data, state="FAILED")
                    return result_data

                task_id = await workflow_manager.submit(
                    kind="execute_code",
                    owner=principal,
                    request={
                        "session_id": session_id,
                        "panel_id": panel_id,
                        "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
                        **durable_identity,
                    },
                    runner=run_code_execution,
                    scope_key=_workflow_scope(session_id, panel_id),
                )
                task_ref["id"] = task_id
                launch_result_delivery(task_id, panel_id)

            elif msg_type == "resume_run":
                run_id = data.get("run_id")
                if not run_id:
                    await send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": {
                                "type": "ValidationError",
                                "message": "缺少要继续的任务 ID",
                            },
                            "panel_id": panel_id,
                        },
                    })
                    continue

                context = _get_context(session_id, panel_id)
                await history.create_session(session_id, title="", user_id=user_id)
                await history.create_panel(session_id, panel_id, user_id=user_id)
                task_ref: dict[str, str] = {}

                async def run_resume(_progress):
                    try:
                        response = await orchestrator.resume_run(run_id)
                        result_data = response.model_dump(mode="json")
                    except Exception as exc:
                        logger.error(
                            "Resume run %s failed with %s",
                            run_id,
                            type(exc).__name__,
                        )
                        result_data = {
                            "request_id": task_ref["id"],
                            "success": False,
                            "error": public_generation_error(exc),
                        }

                    if result_data.get("success") and result_data.get("code"):
                        context.current_code = result_data["code"]
                        await history.update_panel_code(
                            panel_id,
                            result_data["code"],
                            result_data.get("params"),
                        )

                    result_data["panel_id"] = panel_id
                    result_data["task_id"] = task_ref["id"]
                    if result_data.get("success") and result_data.get("code"):
                        snapshot = await history.create_model_snapshot(
                            panel_id,
                            result_data,
                            source="resume_run",
                            prompt="继续上次任务",
                        )
                        result_data["snapshot_id"] = snapshot["id"]
                        result_data["version"] = snapshot["version"]
                        _attach_snapshot_identity(result_data, snapshot)
                    await claim_request_owner(
                        result_data.get("request_id"),
                        principal,
                        revision_id=result_data.get("snapshot_id"),
                    )
                    error_message = (result_data.get("error") or {}).get(
                        "message",
                        "未知错误",
                    )
                    assistant_content = (
                        "已继续上次任务"
                        if result_data.get("success")
                        else f"继续上次任务失败: {error_message}"
                    )
                    await history.save_message(
                        panel_id,
                        "assistant",
                        assistant_content,
                        result=result_data,
                    )
                    await history.touch_session(session_id)
                    if not result_data.get("success") and result_data.get(
                        "request_id"
                    ) == task_ref["id"]:
                        return LocalWorkflowOutcome(result_data, state="FAILED")
                    return result_data

                task_id = await workflow_manager.submit(
                    kind="resume_run",
                    owner=principal,
                    request={
                        "session_id": session_id,
                        "panel_id": panel_id,
                        "source_run_id": run_id,
                    },
                    runner=run_resume,
                    scope_key=_workflow_scope(session_id, panel_id),
                )
                task_ref["id"] = task_id
                launch_result_delivery(task_id, panel_id)

            elif msg_type == "restore_context":
                # Restore backend context from history for a panel
                restore_code = data.get("code")
                if restore_code:
                    context = _get_context(session_id, panel_id)
                    context.current_code = restore_code
                    logger.info(f"Restored context for {session_id}/{panel_id}")
                if not settings.durable_api_cutover_enabled:
                    await _replay_latest_run(send_json, session_id, panel_id)

            elif msg_type == "restore_task":
                task_id = data.get("task_id")
                if task_id:
                    run = await workflow_manager.get(task_id, owner=principal)
                else:
                    run = await workflow_manager.get_latest_for_scope(
                        _workflow_scope(session_id, panel_id),
                        owner=principal,
                    )
                if run is None:
                    await send_json({
                        "type": "task_status",
                        "data": {
                            "task_id": task_id,
                            "panel_id": panel_id,
                            "status": "not_found",
                        },
                    })
                    continue

                task_id = run["id"]
                replay_delivery = await make_on_step(
                    panel_id,
                    {"id": task_id},
                )
                for event in await local_runs.list_events(task_id):
                    if event["event_type"] != "progress":
                        continue
                    step = StepUpdate.model_validate(event["payload"])
                    await replay_delivery(step)
                await send_json({
                    "type": "task_status",
                    "data": {
                        "task_id": task_id,
                        "panel_id": panel_id,
                        "status": run["state"].lower(),
                    },
                })
                if run["state"] not in local_runs.TERMINAL_STATES:
                    workflow_manager.attach_progress_delivery(
                        task_id,
                        replay_delivery,
                    )
                    launch_result_delivery(
                        task_id,
                        panel_id,
                        progress_delivery=replay_delivery,
                    )
                else:
                    result_data = run["result"]
                    if result_data is not None:
                        result_data = dict(result_data)
                        result_data["panel_id"] = panel_id
                        result_data["task_id"] = task_id
                        await send_json({
                            "type": "generation_result",
                            "data": result_data,
                        })

            elif msg_type == "cancel":
                task_id = data.get("task_id")
                if not task_id:
                    active = await local_runs.find_latest_for_scope(
                        _workflow_scope(session_id, panel_id),
                        principal,
                        active_only=True,
                    )
                    task_id = active["id"] if active else None
                if task_id:
                    cancelled = await workflow_manager.request_cancel(
                        task_id,
                        owner=principal,
                    )
                else:
                    cancelled = False
                await send_json({
                    "type": "generation_result",
                    "data": {
                        "success": False,
                        "error": (
                            {"type": "Cancelled", "message": "用户取消"}
                            if cancelled
                            else {
                                "type": "TaskNotFound",
                                "message": "没有正在运行的任务。",
                            }
                        ),
                        "panel_id": panel_id,
                        "task_id": task_id,
                    },
                })

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
