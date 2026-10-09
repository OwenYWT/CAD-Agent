import asyncio
import logging
import re
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import text as sql_text

from app.agent import run_store
from app.agent.conversation import ConversationContext
from app.api.auth import verify_ws_token, get_ws_user_id, rate_limiter, websocket_auth_token
from app.api.error_messages import public_generation_error
from app.models.schemas import DurableRequestIdentity
from app.domain.requirement_basis import RequirementBasisV1
from app.config import settings
from app.db import tenant_transaction
from app.freecad.state_contract import (
    ParameterStateError,
    compile_parameter_operation_plan,
    read_verified_state_artifact,
)
from app.freecad.selection import SelectionContextV1
from app.storage import history
from app.services.durable_submission import (
    ensure_workspace_identity,
    recover_session_submission,
    resolve_native_revision_restore,
    submit_durable_workflow,
)
from app.services.operation_resolution import (
    OperationResolutionError,
    load_revision_source_inventory,
    resolve_browser_submission,
)
from app.repositories.revisions import StaleBaseRevision
from app.services.run_state import IdempotencyConflict
from app.temporal_client import TemporalWorkerUnavailable
from app.workflows.temporal import (
    FreeCADStructuredModificationV1,
    cancel_mcad_workflow,
)

logger = logging.getLogger(__name__)


class ModifyPartValidationError(ValueError):
    pass


class MissingBaseRevisionError(ModifyPartValidationError):
    pass


class PartContextMismatchError(ModifyPartValidationError):
    pass

from collections import OrderedDict

_MAX_SESSIONS = 500
sessions: "OrderedDict[str, dict[str, ConversationContext]]" = OrderedDict()

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9\-]{1,128}$")


def _submission_outcome(exc):
    return "rejected" if isinstance(exc, (
        ValueError, PermissionError, KeyError, StaleBaseRevision,
        IdempotencyConflict, TemporalWorkerUnavailable,
    )) else "unknown"


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
    if not settings.durable_control_plane_enabled:
        await websocket.close(
            code=1013,
            reason="Durable control plane is required",
        )
        return

    # Auth check
    token, auth_protocol = websocket_auth_token(websocket)
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
    principal_context = await reconcile_principal(principal_context)
    bind_principal(principal_context)

    # Ownership check: a logged-in user must not attach to a session_id that another
    # user already owns (otherwise they could write panels/messages into it). A brand
    # new session_id, a NULL-owner session, and local-dev anonymous mode (user_id is
    # None) are all allowed; session_writable_by_user encodes exactly that.
    if not await history.session_writable_by_user(session_id, user_id):
        await websocket.close(code=4003, reason="Session belongs to another user")
        return

    await websocket.accept(subprotocol=auth_protocol)

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
        structured_modification = None
        revision_restore = None
        restore_context = None
        if msg_type == "user_message":
            text = str(data.get("text") or "")
            capability = data.get("capability", "auto")
            operation_intent = data.get("operation_intent")
            if not text or len(text) > 10000:
                raise ValueError("提示词为空或超过长度限制")
            if capability not in {"auto", "cad", "dxf"}:
                raise ValueError(
                    "该能力需要结构化输入或已有产物，请使用 "
                    "/api/capability-actions"
                )
            if operation_intent not in {None, "generate", "modify"}:
                raise ValueError("operation_intent must be generate or modify")
        elif msg_type == "modify_part":
            part_name = str(data.get("part_name") or "")
            part_id = str(data.get("part_id") or "")
            instruction = str(data.get("instruction") or "")
            existing_code = (
                str(data["code"])
                if data.get("code") is not None
                else None
            )
            base_revision_id = str(data.get("base_revision_id") or "")
            assembly_parts = data.get("assembly_parts") or []
            if (
                not part_name
                or not instruction
                or len(instruction) > 10000
                or (existing_code is not None and len(existing_code) > 50000)
            ):
                raise ValueError(
                    "零件名、修改指令或当前 MCAD 代码长度无效"
                )
            if not base_revision_id:
                raise MissingBaseRevisionError("当前版本缺少基线版本，请先切换到可恢复的历史版本")
            if part_id:
                known_part_ids = {
                    str(part.get("part_id") or "")
                    for part in assembly_parts
                    if isinstance(part, dict)
                }
                if part_id not in known_part_ids:
                    raise PartContextMismatchError("选中的零件不在当前装配上下文中")
        elif msg_type == "execute_code":
            submitted_code = str(data.get("code") or "")
            if not submitted_code or len(submitted_code) > 50000:
                raise ValueError("代码为空或超过长度限制")
        elif msg_type == "restore_revision":
            try:
                source_revision_id = UUID(str(data.get("source_revision_id") or ""))
            except ValueError as exc:
                raise ValueError("历史版本 ID 无效") from exc
            if any(key in data for key in ("code", "inputs", "source_artifact_id", "source_sha256")):
                raise ValueError("版本恢复只接受历史版本 ID，不接受客户端文件或源码")
        elif msg_type == "modify_parameters":
            raw_updates = data.get("updates")
            if not isinstance(raw_updates, list):
                raise ParameterStateError(
                    "parameter_value_type_invalid",
                    "parameter updates must be a list",
                )
            identifiers = [
                str(item.get("parameter_id") or "")
                for item in raw_updates
                if isinstance(item, dict)
            ]
            if len(identifiers) != len(raw_updates):
                raise ParameterStateError(
                    "parameter_value_type_invalid",
                    "parameter update must be an object",
                )
            if len(identifiers) != len(set(identifiers)):
                raise ParameterStateError(
                    "parameter_duplicate_update",
                    "parameter update batch contains a duplicate ID",
                )
            try:
                structured_modification = FreeCADStructuredModificationV1(
                    expected_state_sha256=str(
                        data.get("expected_state_sha256") or ""
                    ),
                    parameter_updates=tuple(raw_updates),
                )
            except Exception as exc:
                raise ParameterStateError(
                    "parameter_value_type_invalid",
                    "parameter update batch is invalid",
                ) from exc
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

        if msg_type == "modify_part" and UUID(base_revision_id) != expected_base_revision_id:
            raise PartContextMismatchError("零件所属版本与本次修改基线不一致，请重新同步并选择零件")

        operation_resolution = None
        if msg_type == "restore_revision":
            revision_restore, restore_context = await resolve_native_revision_restore(
                principal_context, project_id=project_id, branch_id=branch_id,
                expected_base_revision_id=expected_base_revision_id,
                source_revision_id=source_revision_id, panel_id=panel_id,
            )
        if msg_type in {"user_message", "modify_part", "modify_parameters"}:
            inventory = await load_revision_source_inventory(
                principal_context,
                project_id=project_id,
                revision_id=UUID(str(data["source_candidate_revision_id"])) if data.get("source_candidate_revision_id") else expected_base_revision_id,
            )
            if msg_type == "modify_parameters":
                requested_operation = "modify"
                rule = "explicit_parameter_edit"
                browser_code = None
            elif msg_type == "modify_part":
                requested_operation = "modify"
                rule = "explicit_modify_part"
                browser_code = existing_code
            else:
                requested_operation = data.get("operation_intent")
                if requested_operation is not None:
                    rule = "explicit_ui_intent"
                elif inventory.fcstd or inventory.cadquery:
                    rule = "legacy_editable_base_present"
                else:
                    rule = "legacy_empty_panel"
                browser_code = None
            operation_resolution = resolve_browser_submission(
                requested_operation=requested_operation,
                rule=rule,
                panel_id=panel_id,
                base_revision_id=expected_base_revision_id,
                inventory=inventory,
                browser_code=browser_code,
                generation_backend=(
                    "cadquery"
                    if data.get("capability", "auto") == "dxf"
                    else "auto"
                ),
            )
            if data.get("source_candidate_revision_id"):
                if msg_type != "user_message" or operation_resolution.modeling_backend != "freecad":
                    raise ValueError("候选续改只支持明确的原生建模请求")
                source_revision = UUID(str(data["source_candidate_revision_id"]))
                context = operation_resolution.operation_context.model_copy(update={
                    "source_candidate_revision_id": source_revision,
                })
                operation_resolution = operation_resolution.__class__(operation=operation_resolution.operation,
                    modeling_backend=operation_resolution.modeling_backend, existing_code=operation_resolution.existing_code,
                    operation_context=context)
            if msg_type == "modify_parameters":
                if operation_resolution.modeling_backend != "freecad":
                    raise ParameterStateError(
                        "parameter_state_missing",
                        "structured parameter editing requires an FCStd base",
                    )
                async with tenant_transaction(
                    principal_context.tenant_id,
                    principal_context.principal_id,
                ) as connection:
                    state_rows = [
                        dict(row)
                        for row in (
                            await connection.execute(
                                sql_text(
                                    """
                                    SELECT id, artifact_kind, object_key,
                                           size_bytes, sha256
                                    FROM artifacts
                                    WHERE tenant_id=:tenant_id
                                      AND project_id=:project_id
                                      AND revision_id=:revision_id
                                      AND lower(artifact_kind)='state'
                                    ORDER BY created_at, id
                                    """
                                ),
                                {
                                    "tenant_id": principal_context.tenant_id,
                                    "project_id": project_id,
                                    "revision_id": expected_base_revision_id,
                                },
                            )
                        ).mappings().all()
                    ]
                state, _ = await read_verified_state_artifact(
                    state_rows,
                    require_v2=True,
                    expected_sha256=(
                        structured_modification.expected_state_sha256
                    ),
                )
                compile_parameter_operation_plan(
                    state,
                    structured_modification.model_dump(mode="json"),
                    output_formats=("step", "stl"),
                )

        if msg_type == "user_message":
            operation = operation_resolution.operation
            objective = text
            if data.get("capability", "auto") == "dxf":
                objective = (
                    "只生成 1:1 的二维 DXF 图纸，不生成三维模型。"
                    "请将下述需求规划为 profile_2d，并输出 DXF：\n"
                    + text
                )
            code = operation_resolution.existing_code
            output_formats = (
                ["dxf"]
                if data.get("capability", "auto") == "dxf"
                else ["step", "stl"]
            )
            profile = data.get("manufacturing_profile")
        elif msg_type == "modify_part":
            operation = operation_resolution.operation
            objective = (
                f"修改零件 {data.get('part_name', '')}: "
                f"{data.get('instruction', '')}"
            )
            code = operation_resolution.existing_code
            output_formats = ["step", "stl"]
            profile = None
        elif msg_type == "execute_code":
            operation = "execute"
            objective = "执行用户提交的 MCAD 代码"
            code = str(data.get("code") or "")
            output_formats = ["step", "stl"]
            profile = None
        elif msg_type == "modify_parameters":
            operation = "modify"
            objective = "更新已验证的 FreeCAD 参数：" + "、".join(
                update.parameter_id
                for update in structured_modification.parameter_updates
            )
            code = None
            output_formats = ["step", "stl"]
            profile = None
        elif msg_type == "restore_revision":
            operation = "modify"
            objective = f"恢复历史原生版本 {source_revision_id}，保留原始特征和参数"
            code = None
            output_formats = ["step", "stl"]
            profile = None
        else:
            raise ValueError(f"unsupported cutover message: {msg_type}")

        submission = await submit_durable_workflow(
            principal_context,
            **({"expected_state_version": data["expected_state_version"]} if "expected_state_version" in data else {}),
            project_id=project_id,
            branch_id=branch_id,
            expected_base_revision_id=expected_base_revision_id,
            idempotency_key=str(durable_identity["idempotency_key"]),
            operation=operation,
            objective=objective,
            output_formats=output_formats,
            code=code,
            manufacturing_profile=profile,
            modeling_backend=(
                "freecad" if revision_restore is not None else (
                    operation_resolution.modeling_backend
                    if operation_resolution is not None else None
                )
            ),
            operation_context=(
                restore_context if revision_restore is not None else (
                    operation_resolution.operation_context
                    if operation_resolution is not None else None
                )
            ),
            structured_modification=structured_modification,
            **({"requirement_basis": RequirementBasisV1.model_validate(data["requirement_basis"])}
               if data.get("requirement_basis") is not None else {}),
            **({"selection_context": SelectionContextV1.model_validate(data["selection_context"])}
               if data.get("selection_context") is not None else {}),
            **({"revision_restore": revision_restore} if revision_restore else {}),
        )
        await send_json({
            "type": "task_submitted",
            "data": {
                "workflow_run_id": str(submission.workflow_run_id),
                "submission_id": str(durable_identity["idempotency_key"]),
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
                "submission_id": str(durable_identity["idempotency_key"]),
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
                "modify_parameters",
                "restore_task",
                "restore_revision",
                "cancel",
                "resume_run",
                "recover_submission",
            }:
                if not await history.panel_writable_by_session(panel_id, session_id, user_id):
                    await websocket.close(code=4003, reason="Panel belongs to another session")
                    return

            if msg_type == "recover_submission":
                try:
                    receipt = await recover_session_submission(principal_context, session_id=session_id,
                        panel_id=panel_id, idempotency_key=data.get("idempotency_key"))
                    await send_json({"type":"task_submitted" if receipt else "submission_not_found",
                        "data":receipt or {"panel_id":panel_id,"submission_id":data["idempotency_key"]}})
                except Exception as exc:
                    await send_json({"type":"generation_result","data":{"success":False,
                        "panel_id":panel_id,"submission_id":data.get("idempotency_key"),
                        "submission_outcome":"rejected" if isinstance(exc,(ValueError,PermissionError,KeyError)) else "unknown",
                        "error":{"type":type(exc).__name__,"message":"原请求状态读取失败，请恢复连接后继续查询。"}}})
                continue

            if msg_type in {
                "user_message",
                "modify_part",
                "execute_code",
                "modify_parameters",
                "resume_run",
                "restore_revision",
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
                            "submission_id": data.get("idempotency_key"),
                            "submission_outcome": "rejected",
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
                        "submission_id": data.get("idempotency_key"),
                        "submission_outcome": "rejected",
                        "error": {"type": "RateLimitError", "message": "请求过于频繁，请稍后再试"},
                        "panel_id": panel_id,
                    },
                })
                continue

            if msg_type in {
                "user_message",
                "modify_part",
                "modify_parameters",
                "execute_code",
                "restore_revision",
            }:
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
                    error = (
                        exc.public_error()
                        if isinstance(exc, OperationResolutionError)
                        else {
                            "type": exc.code,
                            "message": str(exc),
                            "details": {},
                            "retryable": False,
                        }
                        if isinstance(exc, ParameterStateError)
                        else public_generation_error(exc)
                    )
                    if type(exc) is ValueError and error["type"] == "ValueError":
                        error["type"] = "ValidationError"
                    await send_json({
                        "type": "generation_result",
                        "data": {
                            "success": False,
                            "error": error,
                            "submission_id": str(durable_identity.get("idempotency_key") or ""),
                            "submission_outcome": _submission_outcome(exc),
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
                            "submission_id": str(durable_identity.get("idempotency_key") or ""),
                            "submission_outcome": _submission_outcome(exc),
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
