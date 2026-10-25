"""Planning use cases; Temporal names live in the adapter."""
from __future__ import annotations
import hashlib
import json
import re
from typing import Any
from temporalio.exceptions import ApplicationError
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

def requirements_objective(request) -> str:
    """The same frozen user inputs reach new-design and modification planning."""
    basis = request.operation_context.requirement_basis if request.operation_context else None
    return request.objective + ('\n\n' + basis.planning_context() if basis else '')


def requirements_sources(request) -> list[dict]:
    sources = [{'source_input_id':'objective', 'source_kind':'user', 'text':request.objective}]
    basis = request.operation_context.requirement_basis if request.operation_context else None
    if basis:
        for key in ('dimensions', 'fit_notes'):
            value = getattr(basis, key)
            if value:
                if key == 'dimensions' and _dimension_echo(request.objective, value):
                    continue
                sources.append({'source_input_id':'basis-'+key.replace('_','-'), 'source_kind':'confirmed', 'text':value})
    return sources


def _dimension_echo(objective: str, value: str) -> bool:
    """Discard only a provable duplicate of the unchanged original input.

    Older intake clients extracted a bare, ordered list of numbers. That list
    cannot become a new property requirement. Any edited value or labelled
    supplementary condition remains an independent confirmed source.
    """
    if value.strip() == objective.strip():
        return True
    quantity = r'\d+(?:\.\d+)?(?:\s*[×xX*]\s*\d+(?:\.\d+)?){0,2}\s*(?:mm|cm|毫米|厘米)'
    if not re.fullmatch(rf'\s*{quantity}(?:\s*[；;]\s*{quantity})*\s*', value, re.I):
        return False
    normalize = lambda text: re.sub(r'\s+', '', text).lower()
    return ([normalize(m.group()) for m in re.finditer(quantity, objective, re.I)]
            == [normalize(m.group()) for m in re.finditer(quantity, value, re.I)])


def modification_acceptance_options(request, payload, *, sources, base_state):
    # New 3D jobs share source semantics across backends. The workflow patch
    # preserves the input/decisions of released CadQuery histories; DXF/SVG-only
    # jobs retain their separate 2D validation policy.
    enabled = request.modeling_backend == 'freecad' or (
        payload.get('all_3d_acceptance_v1') and bool(set(request.output_formats) & {'step', 'stl'}))
    if not enabled or not payload.get('engineering_acceptance_v1'):
        return {}
    result = {'require_acceptance': True}
    if payload.get('acceptance_bindings_v2'):
        result['acceptance_version'] = 'engineering-acceptance.v2'
        if sources is not None:
            result.update(requirement_sources=sources, verified_state=base_state)
    return result


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
        sources = requirements_sources(request) if payload.get('acceptance_bindings_v2') and payload.get('engineering_acceptance_v1') else None
        if request.operation_context and request.operation_context.native_import:
            source = request.operation_context.native_import
            requirements = CADPlan(description=f'导入 {source.filename}', part_type='custom', dimensions={}, features=[],
                manufacturing_profile=request.manufacturing_profile,
                design_brief={'intent_summary':f'导入 {source.filename}', 'artifact_type':'custom',
                    'acceptance_criteria':['文件可打开，实体有效且可重算；产生原生检查点与可审核候选'],
                    'functional_requirements':['FCStd 保留原生特征历史；STEP 保留实体边界，不恢复原始建模历史']})
        elif request.revision_restore is not None:
            requirements = ModificationPlan(
                description=f"从历史 FCStd 恢复版本 {request.revision_restore.source_revision_id}",
                modification_type="revision_restore",
            )
        elif request.operation == "generate":
            requirements = await durable_planner.requirements_generation(
                requirements_objective(request),
                **({"require_acceptance": True} if payload.get("engineering_acceptance_v1") else {}),
                **({'acceptance_version': 'engineering-acceptance.v2'} if payload.get('acceptance_bindings_v2') else {}),
                **({'requirement_sources':sources} if sources is not None else {}),
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
                if sources is not None:
                    from app.services.revision_validation import revision_geometry_baseline
                    from uuid import UUID
                    async with tenant_transaction(tenant_id, principal_id) as connection:
                        baseline = await revision_geometry_baseline(connection, request.branch_id, UUID(base_state['revision_id']))
                    base_state = {**base_state, 'verified_acceptance':baseline}
                model_context = json.dumps(
                    bounded_agent_context(base_state),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            else:
                model_context = request.existing_code or ""
            acceptance_options = modification_acceptance_options(request, payload, sources=sources, base_state=base_state)
            requirements = await durable_planner.requirements_modification(
                model_context,
                requirements_objective(request),
                **acceptance_options,
            )
            if acceptance_options.get('require_acceptance') and requirements.acceptance is None:
                raise ApplicationError('修改任务缺少必需的工程验收合同',
                    type='engineering_verification_unavailable', non_retryable=True)
        acceptance = (requirements.design_brief.acceptance
            if isinstance(requirements, CADPlan) and requirements.design_brief
            else getattr(requirements, 'acceptance', None))
        if acceptance and acceptance.verification_limits:
            raise ApplicationError(
                '当前尚不能核验以下必需工程条件：' + '；'.join(acceptance.verification_limits),
                type='engineering_verification_unavailable', non_retryable=True)
        result = {
            "step_key": step_key,
            "operation": request.operation,
            "requirements": requirements.model_dump(mode="json"),
            "base_state": base_state,
        }
        if acceptance and acceptance.schema_version == 'engineering-acceptance.v2':
            from app.validation.requirement_binding import validate_requirement_bindings
            binding = validate_requirement_bindings(requirements_objective(request), acceptance, base_state, requirement_sources=sources)
            result.update(requirement_bindings=binding, binding_digest=canonical_sha256(binding),
                requirements_digest=binding['requirements_digest'], acceptance_version=acceptance.schema_version,
                comparison_policy_id=acceptance.comparison_policy_id)
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
                request.structured_modification is None and request.revision_restore is None
                and not (request.operation_context and request.operation_context.native_import)))
        if request.operation_context and request.operation_context.native_import:
            imported_plan = plan.temporal_payload()
            imported_plan['confirmation_policy'] = 'none'
            for gate in imported_plan['validation_policy'].values():
                gate['repair_budget'] = 0
            plan = AgentPlan.model_validate(imported_plan)
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
        original_acceptance = (requirements.design_brief.acceptance if isinstance(requirements, CADPlan)
            and requirements.design_brief else getattr(requirements, 'acceptance', None))
        if original_acceptance and original_acceptance.schema_version == 'engineering-acceptance.v2':
            if plan.design_brief.acceptance is None or plan.design_brief.acceptance.digest() != original_acceptance.digest():
                raise ValueError('planning cannot alter the bound engineering acceptance')
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
        if payload.get('repair_accounting_v2'):
            from app.config import settings
            from app.workflows.work_accounting import WorkBudgetPolicy
            result['work_budget_policy'] = WorkBudgetPolicy(
                max_work_units=settings.agent_max_work_units,
                max_repair_attempts=settings.agent_max_repair_attempts,
                max_checkpoint_turns=settings.agent_max_checkpoint_turns,
                max_normal_tool_calls=settings.agent_max_normal_tool_calls,
            ).model_dump(mode='json')
        if payload.get('solid_count_policy_v2'):
            from app.agent.durable_plan import resolve_solid_count_policy
            result['solid_count_policy'] = resolve_solid_count_policy(plan_payload)
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
