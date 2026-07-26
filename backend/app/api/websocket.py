import logging
import re

from fastapi import WebSocket, WebSocketDisconnect

from app.agent import run_store
from app.agent.orchestrator import ConversationContext, Orchestrator
from app.agent.recovery import ensure_run_resume_available, recover_running_run, sanitize_incomplete_steps
from app.api.auth import verify_ws_token, get_ws_user_id, rate_limiter
from app.api.error_messages import public_generation_error
from app.models.schemas import StepUpdate
from app.storage import history
from app.storage.file_ownership import claim_request_owner

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


async def _send_artifact_updates(websocket: WebSocket, result_data: dict, panel_id: str) -> None:
    for artifact in _artifact_update_payload(result_data, panel_id):
        await websocket.send_json({"type": "artifact_update", "data": artifact})


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


async def _replay_latest_run(websocket: WebSocket, session_id: str, panel_id: str) -> None:
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
    await websocket.send_json({"type": "run_created", "data": _run_payload(run, panel_id)})
    for step in await run_store.list_steps(run["id"]):
        await websocket.send_json({"type": "agent_step", "data": _agent_step_payload_from_record(step, panel_id)})
    for artifact in await run_store.list_artifacts(run["id"]):
        await websocket.send_json({
            "type": "artifact_update",
            "data": {
                "request_id": run.get("result_request_id"),
                "panel_id": panel_id,
                "artifact_type": artifact["artifact_type"],
                "path": artifact["path"],
            },
        })


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

    # Ownership check: a logged-in user must not attach to a session_id that another
    # user already owns (otherwise they could write panels/messages into it). A brand
    # new session_id, a NULL-owner session, and local-dev anonymous mode (user_id is
    # None) are all allowed; session_writable_by_user encodes exactly that.
    if not await history.session_writable_by_user(session_id, user_id):
        await websocket.close(code=4003, reason="Session belongs to another user")
        return

    await websocket.accept()

    orchestrator = _get_orchestrator()

    async def make_on_step(panel_id: str, run_id: str | None = None):
        async def on_step(step: StepUpdate):
            payload = step.model_dump()
            payload["panel_id"] = panel_id
            await websocket.send_json({"type": "step_update", "data": payload})
            await websocket.send_json({"type": "agent_step", "data": _agent_step_payload(step, panel_id, run_id=run_id)})
        return on_step

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            panel_id = data.get("panel_id", "default")

            if msg_type in {"user_message", "modify_part", "execute_code", "resume_run"}:
                if not await history.panel_writable_by_session(panel_id, session_id, user_id):
                    await websocket.close(code=4003, reason="Panel belongs to another session")
                    return

            # Rate limit per WebSocket message
            try:
                await rate_limiter.check(websocket, token)
            except Exception:
                await websocket.send_json({
                    "type": "generation_result",
                    "data": {
                        "success": False,
                        "error": {"type": "RateLimitError", "message": "请求过于频繁，请稍后再试"},
                        "panel_id": panel_id,
                    },
                })
                continue

            if msg_type == "user_message":
                text = data.get("text", "")
                capability = data.get("capability", "auto")
                manufacturing_profile = data.get("manufacturing_profile")
                if not text or len(text) > 10000:
                    await websocket.send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": {"type": "ValidationError", "message": "提示词为空或超过长度限制"},
                            "panel_id": panel_id,
                        },
                    })
                    continue
                if capability not in {"auto", "cad", "dxf"}:
                    await websocket.send_json({
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
                run = await run_store.create_run(
                    session_id,
                    effective_text,
                    panel_id=panel_id,
                    capability=capability,
                )
                on_step = await make_on_step(panel_id, run_id=run["id"])
                await websocket.send_json({"type": "run_created", "data": _run_created_payload(run, panel_id)})

                await history.create_session(session_id, title=text[:80], user_id=user_id)
                await history.create_panel(session_id, panel_id, user_id=user_id)
                await history.save_message(panel_id, "user", text)

                try:
                    if manufacturing_profile is not None:
                        result = await orchestrator.handle_message(
                            context,
                            effective_text,
                            on_step=on_step,
                            manufacturing_profile=manufacturing_profile,
                            run_id=run["id"],
                        )
                    else:
                        result = await orchestrator.handle_message(
                            context, effective_text, on_step=on_step, run_id=run["id"]
                        )
                except WebSocketDisconnect:
                    raise
                except Exception as e:
                    logger.error(f"Generation error for {session_id}/{panel_id}: {e}", exc_info=True)
                    error_data = {
                        "success": False,
                        "error": public_generation_error(e),
                        "panel_id": panel_id,
                    }
                    await history.save_message(
                        panel_id,
                        "assistant",
                        f"生成失败: {error_data['error']['message']}",
                        result=error_data,
                    )
                    await history.touch_session(session_id)
                    await websocket.send_json({"type": "generation_result", "data": error_data})
                    continue

                result_data = result.model_dump()
                result_data["panel_id"] = panel_id
                claim_request_owner(result.request_id, principal)
                if result.success and result.code:
                    snapshot = await history.create_model_snapshot(
                        panel_id, result_data, source="generation", prompt=text
                    )
                    result_data["snapshot_id"] = snapshot["id"]
                    result_data["version"] = snapshot["version"]

                if result.needs_confirmation:
                    assistant_content = "设计简报需要确认"
                elif result.success:
                    assistant_content = "CAD 模型已生成"
                else:
                    error_message = result.error.get("message", "") if result.error else "未知错误"
                    assistant_content = f"生成失败: {error_message}"
                await history.save_message(panel_id, "assistant", assistant_content, result=result_data)
                if result.success and result.code:
                    params_dict = (
                        {k: v.model_dump() for k, v in result.params.items()}
                        if result.params
                        else None
                    )
                    await history.update_panel_code(panel_id, result.code, params_dict)
                await history.touch_session(session_id)

                await _send_artifact_updates(websocket, result_data, panel_id)
                await websocket.send_json({"type": "generation_result", "data": result_data})

            elif msg_type == "modify_part":
                part_name = data.get("part_name", "")
                instruction = data.get("instruction", "")
                if not part_name or not instruction or len(instruction) > 10000:
                    await websocket.send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": {"type": "ValidationError", "message": "零件名或修改指令为空"},
                            "panel_id": panel_id,
                        },
                    })
                    continue

                context = _get_context(session_id, panel_id)
                on_step = await make_on_step(panel_id)

                # Ensure session+panel rows exist before saving a message — otherwise
                # the FK on messages.panel_id rejects the insert. (user_message does this too.)
                await history.create_session(session_id, title="", user_id=user_id)
                await history.create_panel(session_id, panel_id, user_id=user_id)
                await history.save_message(panel_id, "user", f"修改零件 {part_name}: {instruction}")

                try:
                    result = await orchestrator.modify_assembly_part(
                        context, part_name, instruction, on_step=on_step
                    )
                except WebSocketDisconnect:
                    raise
                except Exception as e:
                    logger.error(f"modify_part error for {session_id}/{panel_id}: {e}", exc_info=True)
                    error_data = {
                        "success": False,
                        "error": public_generation_error(e),
                        "panel_id": panel_id,
                    }
                    await history.save_message(
                        panel_id,
                        "assistant",
                        f"零件修改失败: {error_data['error']['message']}",
                        result=error_data,
                    )
                    await history.touch_session(session_id)
                    await websocket.send_json({"type": "generation_result", "data": error_data})
                    continue

                result_data = result.model_dump()
                result_data["panel_id"] = panel_id
                claim_request_owner(result.request_id, principal)
                if result.success and result.code:
                    snapshot = await history.create_model_snapshot(
                        panel_id,
                        result_data,
                        source="modify_part",
                        prompt=f"修改零件 {part_name}: {instruction}",
                    )
                    result_data["snapshot_id"] = snapshot["id"]
                    result_data["version"] = snapshot["version"]

                msg_content = (
                    f"零件 {part_name} 已修改"
                    if result.success
                    else f"零件修改失败: {result.error.get('message', '') if result.error else '未知错误'}"
                )
                await history.save_message(panel_id, "assistant", msg_content, result=result_data)
                if result.success and result.code:
                    await history.update_panel_code(panel_id, result.code)
                await history.touch_session(session_id)

                await _send_artifact_updates(websocket, result_data, panel_id)
                await websocket.send_json({"type": "generation_result", "data": result_data})

            elif msg_type == "execute_code":
                code = data.get("code", "")
                if not code or len(code) > 50000:
                    await websocket.send_json({
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
                try:
                    response = await orchestrator.execute_code(code)
                except Exception as e:
                    logger.error(f"Execute error: {e}", exc_info=True)
                    result_data = {
                        "success": False,
                        "error": public_generation_error(e),
                        "panel_id": panel_id,
                    }
                    await history.save_message(
                        panel_id,
                        "assistant",
                        "参数修改执行失败",
                        result=result_data,
                    )
                    await history.touch_session(session_id)
                    await websocket.send_json({"type": "generation_result", "data": result_data})
                    continue

                if response.success and response.code:
                    context.current_code = response.code
                    params_dict = (
                        {k: v.model_dump() for k, v in response.params.items()}
                        if response.params
                        else None
                    )
                    await history.update_panel_code(panel_id, response.code, params_dict)

                result_data = response.model_dump()
                result_data["panel_id"] = panel_id
                claim_request_owner(response.request_id, principal)
                if response.success and response.code:
                    snapshot = await history.create_model_snapshot(
                        panel_id, result_data, source="execute_code", prompt="manual code execution"
                    )
                    result_data["snapshot_id"] = snapshot["id"]
                    result_data["version"] = snapshot["version"]
                assistant_content = (
                    "参数修改已执行"
                    if response.success
                    else f"参数修改失败: {response.error.get('message', '') if response.error else '未知错误'}"
                )
                await history.save_message(
                    panel_id,
                    "assistant",
                    assistant_content,
                    result=result_data,
                )
                await history.touch_session(session_id)
                await _send_artifact_updates(websocket, result_data, panel_id)
                await websocket.send_json({"type": "generation_result", "data": result_data})

            elif msg_type == "resume_run":
                run_id = data.get("run_id")
                if not run_id:
                    await websocket.send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": {"type": "ValidationError", "message": "\u7f3a\u5c11\u8981\u7ee7\u7eed\u7684\u4efb\u52a1 ID"},
                            "panel_id": panel_id,
                        },
                    })
                    continue
                context = _get_context(session_id, panel_id)
                await history.create_session(session_id, title="", user_id=user_id)
                await history.create_panel(session_id, panel_id, user_id=user_id)
                try:
                    response = await orchestrator.resume_run(run_id)
                except Exception as e:
                    logger.error("Resume run error: %s", e, exc_info=True)
                    result_data = {
                        "success": False,
                        "error": public_generation_error(e),
                        "panel_id": panel_id,
                    }
                    await history.save_message(panel_id, "assistant", "\u7ee7\u7eed\u4e0a\u6b21\u4efb\u52a1\u5931\u8d25", result=result_data)
                    await history.touch_session(session_id)
                    await websocket.send_json({"type": "generation_result", "data": result_data})
                    continue

                if response.success and response.code:
                    context.current_code = response.code
                    params_dict = (
                        {k: v.model_dump() for k, v in response.params.items()}
                        if response.params
                        else None
                    )
                    await history.update_panel_code(panel_id, response.code, params_dict)

                result_data = response.model_dump()
                result_data["panel_id"] = panel_id
                claim_request_owner(response.request_id, principal)
                if response.success and response.code:
                    snapshot = await history.create_model_snapshot(
                        panel_id, result_data, source="resume_run", prompt="\u7ee7\u7eed\u4e0a\u6b21\u4efb\u52a1"
                    )
                    result_data["snapshot_id"] = snapshot["id"]
                    result_data["version"] = snapshot["version"]
                error_message = response.error.get("message", "") if response.error else "\u672a\u77e5\u9519\u8bef"
                assistant_content = (
                    "\u5df2\u7ee7\u7eed\u4e0a\u6b21\u4efb\u52a1"
                    if response.success
                    else f"\u7ee7\u7eed\u4e0a\u6b21\u4efb\u52a1\u5931\u8d25: {error_message}"
                )
                await history.save_message(panel_id, "assistant", assistant_content, result=result_data)
                await history.touch_session(session_id)
                await _send_artifact_updates(websocket, result_data, panel_id)
                await websocket.send_json({"type": "generation_result", "data": result_data})

            elif msg_type == "restore_context":
                # Restore backend context from history for a panel
                restore_code = data.get("code")
                if restore_code:
                    context = _get_context(session_id, panel_id)
                    context.current_code = restore_code
                    logger.info(f"Restored context for {session_id}/{panel_id}")
                await _replay_latest_run(websocket, session_id, panel_id)

            elif msg_type == "cancel":
                # Acknowledge cancel (actual cancellation is best-effort)
                await websocket.send_json({
                    "type": "generation_result",
                    "data": {
                        "success": False,
                        "error": {"type": "Cancelled", "message": "用户取消"},
                        "panel_id": panel_id,
                    },
                })

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {session_id}")
    except Exception as e:
        logger.error(f"WebSocket error for {session_id}: {e}", exc_info=True)
        try:
            await websocket.close()
        except Exception:
            pass
