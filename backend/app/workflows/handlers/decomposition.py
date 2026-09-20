"""Decompose requirements without owning Worker or CAD version state."""
from __future__ import annotations
from typing import Any
from temporalio.exceptions import ApplicationError
from app.agent.durable_planner import DurableAgentPlanner
from app.db import tenant_transaction
from app.models.schemas import CADPlan

from app.agent.durable_planner import DurableAgentPlanner
from app.workflows.inputs import _agent_v2_request
from app.workflows.errors import planning_error
from app.workflows.logical_steps import (_stored_agent_step_result, _start_agent_logical_step, _complete_agent_logical_step, _fail_agent_logical_step)

async def decompose(payload: dict[str, Any], *, planner: DurableAgentPlanner) -> dict[str, Any]:
    request = _agent_v2_request(payload)
    if request.operation != "generate":
        raise ApplicationError(
            "Only generation plans can be decomposed.",
            type="invalid_agent_decomposition",
            non_retryable=True,
        )
    tenant_id = request.tenant_id
    principal_id = request.principal_id
    workflow_id = request.workflow_run_id
    step_key = "agent-decompose"
    event_type = "agent.decomposition.completed"
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
            step_index=1,
            kind="agent_decompose",
        )
    try:
        plan = CADPlan.model_validate(payload["requirements"])
        if (
            (request.modeling_backend == "freecad" or bool(payload.get("backend_policy_v1")))
            and plan.part_type not in {"assembly", "profile_2d"}
        ):
            decomposition = None
        else:
            decomposition = await planner.decompose_generation(plan)
        result = {
            "step_key": step_key,
            "decomposition": decomposition,
            "skipped": decomposition is None,
        }
    except Exception as exc:
        error = planning_error(exc)
        await _fail_agent_logical_step(
            tenant_id=tenant_id,
            principal_id=principal_id,
            workflow_id=workflow_id,
            step_key=step_key,
            error_code=error.type or "agent_decomposition_failed",
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
