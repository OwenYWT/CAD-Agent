"""Planning use cases; Temporal names live in the adapter."""
from __future__ import annotations
import hashlib
import json
from typing import Any
from app.agent.durable_plan import AgentPlan, normalize_agent_plan_backend, enforce_design_validation
from app.agent.durable_planner import DurableAgentPlanner
from app.db import tenant_transaction
from app.execution.canonical import canonical_sha256
from app.freecad.state_contract import ParameterStateError, compile_parameter_operation_plan
from app.models.schemas import CADPlan, ModificationPlan
from app.workflows.logical_steps import _stored_agent_step_result, _start_agent_logical_step, _complete_agent_logical_step, _fail_agent_logical_step
from app.workflows.inputs import _agent_v2_request
from app.workflows.handlers.legacy_modeling import plan
from app.workflows.revision_inputs import _freecad_revision_artifact
from app.workflows.revision_inputs import _freecad_revision_state
from app.workflows.errors import planning_error

async def agent_requirements(payload: dict[str, Any], *, durable_planner: DurableAgentPlanner) -> dict[str, Any]:
    request = _agent_v2_request(payload)
    tenant_id = request.tenant_id
    principal_id = request.principal_id
    workflow_id = request.workflow_run_id
    step_key = "agent-requirements"
    event_type = "agent.requirements.completed"
    async with tenant_transaction(tenant_id, principal_id) as connection:
        replay = await _stored_agent_step_result(
            connection,
            workflow_id=workflow_id,
            event_type=event_type,
            step_key=step_key,
        )
        if replay is not None:
            return replay
        step_id = await _start_agent_logical_step(
            connection,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            step_key=step_key,
            step_index=0,
            kind="agent_requirements",
        )
    try:
        base_state: dict[str, Any] | None = None
        if request.revision_restore is not None:
            requirements = ModificationPlan(
                description=f"从历史 FCStd 恢复版本 {request.revision_restore.source_revision_id}",
                modification_type="revision_restore",
            )
        elif request.operation == "generate":
            requirements = await durable_planner.requirements_generation(
                request.objective + (
                    "\n\n" + request.operation_context.requirement_basis.planning_context()
                    if request.operation_context and request.operation_context.requirement_basis else ""
                )
            )
        else:
            if request.modeling_backend == "freecad":
                if request.structured_modification is not None:
                    state_artifact = await _freecad_revision_artifact(
                        request,
                        "state",
                    )
                    if str(state_artifact["sha256"]) != (
                        request.structured_modification.expected_state_sha256
                    ):
                        raise ParameterStateError(
                            "parameter_state_stale",
                            "parameter state hash differs from the committed revision",
                        )
                base_state = await _freecad_revision_state(request)
                if request.structured_modification is not None:
                    if base_state.get("schema_version") != "freecad-state.v2":
                        raise ParameterStateError(
                            "parameter_state_missing",
                            "structured parameter editing requires freecad-state.v2",
                        )
                    compile_parameter_operation_plan(
                        base_state,
                        request.structured_modification.model_dump(mode="json"),
                        output_formats=request.output_formats,
                    )
                    requirements = ModificationPlan(
                        description=request.objective if request.structured_modification.native_edits else "更新已验证的 FreeCAD 参数",
                        modification_type="structured_native_edit" if request.structured_modification.native_edits else "structured_parameter_edit",
                        target_params={
                            update.parameter_id: update.value
                            for update in request.structured_modification.parameter_updates
                        },
                    )
                    result = {
                        "step_key": step_key,
                        "operation": request.operation,
                        "requirements": requirements.model_dump(mode="json"),
                        "base_state": base_state,
                    }
                    async with tenant_transaction(
                        tenant_id,
                        principal_id,
                    ) as connection:
                        return await _complete_agent_logical_step(
                            connection,
                            tenant_id=tenant_id,
                            workflow_id=workflow_id,
                            step_id=step_id,
                            event_type=event_type,
                            result=result,
                        )
                from app.freecad.semantic_state import bounded_agent_context
                model_context = json.dumps(
                    bounded_agent_context(base_state),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            else:
                model_context = request.existing_code or ""
            requirements = await durable_planner.requirements_modification(
                model_context,
                request.objective,
            )
        result = {
            "step_key": step_key,
            "operation": request.operation,
            "requirements": requirements.model_dump(mode="json"),
            "base_state": base_state,
        }
        if base_state and (base_state.get('engineering_evidence') or base_state.get('selection_context')):
            from app.llm import get_last_chat_completion_provenance
            provider = get_last_chat_completion_provenance()
            if provider is None:
                raise ValueError('原生文档 Agent 规划缺少真实模型调用记录')
            provenance = {**provider, 'context_sha256': hashlib.sha256(model_context.encode('utf-8')).hexdigest()}
            if base_state.get('selection_context'):
                result['native_context'] = {**provenance, 'selection_context': base_state['selection_context']}
            if base_state.get('engineering_evidence'):
                result['engineering_context'] = {**provenance,
                    'references':[e['source'] for e in base_state['engineering_evidence']]}
    except Exception as exc:
        error = planning_error(exc)
        await _fail_agent_logical_step(
            tenant_id=tenant_id,
            principal_id=principal_id,
            workflow_id=workflow_id,
            step_key=step_key,
            error_code=error.type or "agent_requirements_failed",
            error_message=str(error),
        )
        raise error from exc
    async with tenant_transaction(tenant_id, principal_id) as connection:
        return await _complete_agent_logical_step(
            connection,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            step_id=step_id,
            event_type=event_type,
            result=result,
        )


async def agent_plan(payload: dict[str, Any], *, durable_planner: DurableAgentPlanner) -> dict[str, Any]:
    request = _agent_v2_request(payload)
    tenant_id = request.tenant_id
    principal_id = request.principal_id
    workflow_id = request.workflow_run_id
    step_key = "agent-plan"
    event_type = "agent.plan.completed"
    step_index = 2 if request.operation == "generate" else 1
    async with tenant_transaction(tenant_id, principal_id) as connection:
        replay = await _stored_agent_step_result(
            connection,
            workflow_id=workflow_id,
            event_type=event_type,
            step_key=step_key,
        )
        if replay is not None:
            AgentPlan.model_validate(replay["plan"])
            return replay
        step_id = await _start_agent_logical_step(
            connection,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            step_key=step_key,
            step_index=step_index,
            kind="agent_plan",
        )
    try:
        if request.operation == "generate":
            requirements = CADPlan.model_validate(payload["requirements"])
            if (
                (request.modeling_backend == "freecad" or bool(payload.get("backend_policy_v1")))
                and requirements.part_type not in {"assembly", "profile_2d"}
            ):
                plan = durable_planner.compose_freecad_generation(
                    request.objective,
                    requirements,
                    output_formats=request.output_formats,
                )
            else:
                plan = durable_planner.compose_generation(
                    request.objective,
                    requirements,
                    decomposition=payload.get("decomposition"),
                    output_formats=request.output_formats,
                )
        else:
            requirements = ModificationPlan.model_validate(
                payload["requirements"]
            )
            plan = durable_planner.compose_modification(
                request.objective,
                requirements,
                expected_base_revision_id=request.expected_base_revision_id,
                output_formats=request.output_formats,
            )
        if bool(payload.get("backend_policy_v1")):
            plan = normalize_agent_plan_backend(
                operation=request.operation,
                request_modeling_backend=request.modeling_backend,
                plan_candidate=plan,
            )
        if payload.get("validation_repair_v2"):
            plan = enforce_design_validation(plan, autonomous=(
                request.structured_modification is None and request.revision_restore is None))
        if request.revision_restore is not None:
            restored_plan = plan.temporal_payload()
            # Restoration must fail on invalid history, never redesign it.
            for gate in restored_plan["validation_policy"].values():
                gate["repair_budget"] = 0
            restored_plan["design_brief"]["acceptance_criteria"] = [
                "历史 FCStd 可打开并重算，保留原始特征和参数",
                "新导出产物通过必需几何检查，审查提交前不改变当前版本",
            ]
            restored_plan["confirmation_reason"] = "确认从历史原生文件生成恢复候选；当前版本仅在审查提交后改变。"
            plan = AgentPlan.model_validate(restored_plan)
        plan_payload = plan.temporal_payload()
        if plan.modeling_backend == "freecad" or (
            not bool(payload.get("backend_policy_v1"))
            and request.modeling_backend == "freecad"
            and plan.model_kind not in {"assembly", "profile_2d"}
        ):
            plan_payload["modeling_strategy"] = "freecad_operations"
        result = {
            "step_key": step_key,
            "plan": plan_payload,
            "requires_confirmation": (
                plan.confirmation_policy.value == "required"
            ),
            "confirmation_reason": plan.confirmation_reason,
            "plan_hash": canonical_sha256(plan_payload),
        }
    except Exception as exc:
        error = planning_error(exc)
        await _fail_agent_logical_step(
            tenant_id=tenant_id,
            principal_id=principal_id,
            workflow_id=workflow_id,
            step_key=step_key,
            error_code=error.type or "agent_plan_failed",
            error_message=str(error),
        )
        raise error from exc
    async with tenant_transaction(tenant_id, principal_id) as connection:
        return await _complete_agent_logical_step(
            connection,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            step_id=step_id,
            event_type=event_type,
            result=result,
            enter_running=True,
        )
