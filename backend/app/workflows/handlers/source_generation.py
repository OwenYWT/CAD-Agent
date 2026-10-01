"""Source generation use cases; Temporal names live in the adapter."""
from __future__ import annotations
from typing import Any
from uuid import UUID
from sqlalchemy import text
from temporalio.exceptions import ApplicationError
from app.agent.durable_plan import AgentPlan
from app.agent.durable_plan import AgentPlanStep
from app.agent.durable_repair import DurableRepairSourceGenerator, decide_repair
from app.db import tenant_transaction
from app.repositories.agent_candidates import get_generated_source_for_step, record_generated_source
from app.repositories.runs import append_workflow_event
from app.workflows.modeling import DurableModelingSourceGenerator
from app.workflows.logical_steps import _start_agent_logical_step, _fail_agent_logical_step
from app.workflows.inputs import _uuid, _agent_v2_request
from app.workflows.handlers.legacy_modeling import plan
from app.workflows.errors import planning_error

async def agent_generate_source(payload: dict[str, Any], *, durable_modeling: DurableModelingSourceGenerator) -> dict[str, Any]:
    request = _agent_v2_request(payload)
    plan = AgentPlan.model_validate(payload["plan"])
    step = AgentPlanStep.model_validate(payload["step"])
    step_index = int(payload["step_index"])
    candidate_build_id = _uuid(payload, "candidate_build_id")
    async with tenant_transaction(
        request.tenant_id,
        request.principal_id,
    ) as connection:
        replay = await get_generated_source_for_step(
            connection,
            tenant_id=request.tenant_id,
            workflow_id=request.workflow_run_id,
            step_key=step.step_key,
        )
        if replay is not None:
            return {
                "source_id": str(replay["id"]),
                "source_hash": replay["source_hash"],
                "source_code": replay["source_code"],
                "mode": (
                    "2d"
                    if step.output_formats
                    and set(step.output_formats).issubset({"dxf", "svg"})
                    else "3d"
                ),
                "generator_kind": replay["generator_kind"],
                "provenance": {
                    "provider": replay["provider"],
                    "model": replay["model"],
                    "provider_response_id": replay["provider_response_id"],
                    "request_hash": replay["request_hash"],
                    "response_hash": replay["response_hash"],
                    "finish_reason": replay["finish_reason"],
                    "usage": dict(replay["usage"]),
                },
                "replayed": True,
            }
        step_id = await _start_agent_logical_step(
            connection,
            tenant_id=request.tenant_id,
            workflow_id=request.workflow_run_id,
            step_key=step.step_key,
            step_index=step_index,
            kind="agent_model",
        )
    try:
        generated = await durable_modeling.generate_step_source(
            plan=plan,
            step=step,
            requirements=dict(payload["requirements"]),
            previous_source=(
                str(payload["previous_source"])
                if payload.get("previous_source") is not None
                else None
            ),
            step_index=int(payload["plan_step_index"]),
        )
        provenance = generated.provenance
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            recorded = await record_generated_source(
                connection,
                tenant_id=request.tenant_id,
                candidate_build_id=candidate_build_id,
                workflow_id=request.workflow_run_id,
                step_id=step_id,
                predecessor_source_id=(
                    _uuid(payload, "predecessor_source_id")
                    if payload.get("predecessor_source_id")
                    else None
                ),
                input_source_ids=tuple(
                    UUID(str(item))
                    for item in payload.get("input_source_ids") or ()
                ),
                source_code=generated.source_code,
                generator_kind=generated.generator_kind,
                provider=str(provenance["provider"]),
                model=str(provenance["model"]),
                provider_response_id=(
                    str(provenance["provider_response_id"])
                    if provenance.get("provider_response_id")
                    else None
                ),
                request_hash=str(provenance["request_hash"]),
                response_hash=str(provenance["response_hash"]),
                finish_reason=(
                    str(provenance["finish_reason"])
                    if provenance.get("finish_reason")
                    else None
                ),
                usage=dict(provenance.get("usage") or {}),
            )
        return {
            "source_id": str(recorded.source_id),
            "source_hash": recorded.source_hash,
            "source_code": generated.source_code,
            "mode": generated.mode,
            "generator_kind": generated.generator_kind,
            "provenance": provenance,
            "replayed": recorded.replayed,
        }
    except Exception as exc:
        error = planning_error(exc)
        await _fail_agent_logical_step(
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            workflow_id=request.workflow_run_id,
            step_key=step.step_key,
            error_code=error.type or "agent_source_generation_failed",
            error_message=str(error),
        )
        raise error from exc


async def agent_repair_source(payload: dict[str, Any], *, durable_repair: DurableRepairSourceGenerator) -> dict[str, Any]:
    request = _agent_v2_request(payload)
    candidate_build_id = _uuid(payload, "candidate_build_id")
    source_id = _uuid(payload, "source_id")
    failure = dict(payload["failure"])
    repair_index = int(payload["repair_index"])
    original_step_key = str(payload["original_step_key"])
    repair_step_key = str(
        payload.get("repair_step_key")
        or f"repair-{original_step_key}-{repair_index:02d}"
    )
    decision = decide_repair(
        category=str(failure["category"]),
        error_code=str(failure["error_code"]),
        error_message=str(failure["error_message"]),
        operation_id=failure.get("operation_id"),
        runtime_error_type=(
            str(failure["runtime_error_type"])
            if failure.get("runtime_error_type")
            else None
        ),
        repair_count=int(payload.get("gate_repair_index", repair_index)) - 1,
        seen_signatures=tuple(
            str(item) for item in payload.get("seen_signatures") or ()
        ),
    )
    if not decision.repairable:
        raise ApplicationError(
            decision.reason,
            {
                "failure_class": decision.failure_class,
                "strategy": decision.strategy,
                "signature": decision.signature,
            },
            type="agent_repair_not_allowed",
            non_retryable=True,
        )
    async with tenant_transaction(
        request.tenant_id,
        request.principal_id,
    ) as connection:
        replay = await get_generated_source_for_step(
            connection,
            tenant_id=request.tenant_id,
            workflow_id=request.workflow_run_id,
            step_key=repair_step_key,
        )
        if replay is not None:
            return {
                "source_id": str(replay["id"]),
                "source_hash": replay["source_hash"],
                "source_code": replay["source_code"],
                "repair_step_key": repair_step_key,
                "failure_class": replay["generator_kind"].removeprefix(
                    "repair:"
                ),
                "strategy": str(payload.get("strategy") or decision.strategy),
                "signature": decision.signature,
                "provenance": {
                    "provider": replay["provider"],
                    "model": replay["model"],
                    "provider_response_id": replay["provider_response_id"],
                    "request_hash": replay["request_hash"],
                    "response_hash": replay["response_hash"],
                    "finish_reason": replay["finish_reason"],
                    "usage": dict(replay["usage"]),
                },
                "replayed": True,
            }
        source = (
            await connection.execute(
                text(
                    """
                    SELECT source_code, source_hash
                    FROM agent_generated_sources
                    WHERE tenant_id=:tenant_id AND id=:source_id
                      AND candidate_build_id=:candidate_build_id
                    """
                ),
                {
                    "tenant_id": request.tenant_id,
                    "source_id": source_id,
                    "candidate_build_id": candidate_build_id,
                },
            )
        ).mappings().one_or_none()
        if source is None:
            raise ApplicationError(
                "repair input source does not belong to candidate",
                type="agent_repair_source_missing",
                non_retryable=True,
            )
        if str(source["source_hash"]) != str(payload["source_hash"]):
            raise ApplicationError(
                "repair input source hash does not match persisted source",
                type="agent_repair_source_hash_mismatch",
                non_retryable=True,
            )
        step_id = await _start_agent_logical_step(
            connection,
            tenant_id=request.tenant_id,
            workflow_id=request.workflow_run_id,
            step_key=repair_step_key,
            step_index=int(payload["step_index"]),
            kind="agent_repair",
        )
    try:
        repaired = await durable_repair.repair(
            source_code=str(source["source_code"]),
            failure=failure,
            decision=decision,
        )
        provenance = repaired.provenance
        async with tenant_transaction(
            request.tenant_id,
            request.principal_id,
        ) as connection:
            recorded = await record_generated_source(
                connection,
                tenant_id=request.tenant_id,
                candidate_build_id=candidate_build_id,
                workflow_id=request.workflow_run_id,
                step_id=step_id,
                predecessor_source_id=source_id,
                source_code=repaired.source_code,
                generator_kind=f"repair:{repaired.failure_class}",
                provider=str(provenance["provider"]),
                model=str(provenance["model"]),
                provider_response_id=(
                    str(provenance["provider_response_id"])
                    if provenance.get("provider_response_id")
                    else None
                ),
                request_hash=str(provenance["request_hash"]),
                response_hash=str(provenance["response_hash"]),
                finish_reason=(
                    str(provenance["finish_reason"])
                    if provenance.get("finish_reason")
                    else None
                ),
                usage=dict(provenance.get("usage") or {}),
            )
            await append_workflow_event(
                connection,
                tenant_id=request.tenant_id,
                workflow_id=request.workflow_run_id,
                event_type="agent.repair.source_generated",
                payload={
                    "repair_step_key": repair_step_key,
                    "prior_source_id": str(source_id),
                    "source_id": str(recorded.source_id),
                    "prior_source_hash": str(payload["source_hash"]),
                    "source_hash": recorded.source_hash,
                    "failure_class": repaired.failure_class,
                    "strategy": repaired.strategy,
                    "signature": decision.signature,
                    "failure_execution_attempt_id": failure.get(
                        "execution_attempt_id"
                    ),
                    "error_code": failure["error_code"],
                },
            )
        return {
            "source_id": str(recorded.source_id),
            "source_hash": recorded.source_hash,
            "source_code": repaired.source_code,
            "repair_step_key": repair_step_key,
            "failure_class": repaired.failure_class,
            "strategy": repaired.strategy,
            "signature": decision.signature,
            "provenance": provenance,
            "replayed": recorded.replayed,
        }
    except Exception as exc:
        error = planning_error(exc)
        await _fail_agent_logical_step(
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            workflow_id=request.workflow_run_id,
            step_key=repair_step_key,
            error_code=error.type or "agent_repair_failed",
            error_message=str(error),
        )
        raise error from exc
