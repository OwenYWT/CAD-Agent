"""Native generation use cases; Temporal names live in the adapter."""
from __future__ import annotations
import hashlib
from typing import Any
from sqlalchemy import text
from temporalio.exceptions import ApplicationError
from app.agent.durable_plan import AgentPlan
from app.agent.durable_plan import AgentPlanStep
from app.agent.durable_repair import decide_repair
from app.db import tenant_transaction
from app.execution.canonical import canonical_sha256
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.state_contract import compile_parameter_operation_plan
from app.freecad.operation_generator import FreeCADOperationGenerator
from app.repositories.agent_candidates import get_generated_source_for_step, record_generated_source
from app.repositories.runs import append_workflow_event
from app.workflows.logical_steps import _start_agent_logical_step, _fail_agent_logical_step
from app.workflows.inputs import _uuid, _agent_v2_request
from app.workflows.handlers.legacy_modeling import plan
from app.workflows.execution_support import _await_provider_operation
from app.workflows.validation_support import _record_freecad_inspections
from app.workflows.revision_inputs import _revision_restore_operation_plan
from app.workflows.errors import planning_error

async def agent_generate_operations(payload: dict[str, Any], *, freecad_operations: FreeCADOperationGenerator) -> dict[str, Any]:
    request = _agent_v2_request(payload)
    plan = AgentPlan.model_validate(payload["plan"])
    if (
        plan.modeling_backend != "freecad"
        and request.modeling_backend != "freecad"
    ):
        raise ApplicationError(
            "FreeCAD operation generation requires the FreeCAD backend",
            type="invalid_freecad_modeling_backend",
            non_retryable=True,
        )
    step = AgentPlanStep.model_validate(payload["step"])
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
            FreeCADOperationPlan.model_validate_json(replay["source_code"])
            return {
                "source_id": str(replay["id"]),
                "source_hash": replay["source_hash"],
                "source_code": replay["source_code"],
                "mode": "3d",
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
            step_index=int(payload["step_index"]),
            kind="agent_freecad_operations",
        )
    try:
        if request.revision_restore is not None:
            operation_plan = _revision_restore_operation_plan(request)
            source_code = operation_plan.model_dump_json()
            generator_kind = "native-revision-restore-v1"
            provenance = {
                "provider": "cad-agent", "model": generator_kind,
                "provider_response_id": None,
                "request_hash": canonical_sha256(request.revision_restore.model_dump(mode="json")),
                "response_hash": hashlib.sha256(source_code.encode("utf-8")).hexdigest(),
                "finish_reason": "deterministic", "usage": {},
            }
        elif request.structured_modification is not None:
            operation_plan = compile_parameter_operation_plan(
                dict(payload["base_state"]),
                request.structured_modification.model_dump(mode="json"),
                output_formats=request.output_formats,
            )
            source_code = operation_plan.model_dump_json()
            source_hash = hashlib.sha256(
                source_code.encode("utf-8")
            ).hexdigest()
            generator_kind = "structured-native-compiler-v1" if request.structured_modification.native_edits else "structured-parameter-compiler-v1"
            provenance = {
                "provider": "cad-agent",
                "model": generator_kind,
                "provider_response_id": None,
                "request_hash": canonical_sha256({
                    "state_sha256": (
                        request.structured_modification.expected_state_sha256
                    ),
                    "parameter_updates": [
                        update.model_dump(mode="json")
                        for update in request.structured_modification.parameter_updates
                    ],
                    **({'native_edits':[edit.model_dump(mode='json') for edit in request.structured_modification.native_edits]}
                       if request.structured_modification.native_edits else {}),
                }),
                "response_hash": source_hash,
                "finish_reason": "deterministic",
                "usage": {},
            }
        else:
            generated = await _await_provider_operation(freecad_operations.generate(
                plan=plan,
                requirements=dict(payload["requirements"]),
                base_state=(
                    dict(payload["base_state"])
                    if payload.get("base_state") is not None
                    else None
                ),
                output_formats=request.output_formats,
            ))
            source_code = generated.source_code
            generator_kind = generated.generator_kind
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
                source_code=source_code,
                generator_kind=generator_kind,
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
            if not recorded.replayed:
                await _record_freecad_inspections(connection, request, step.step_key, provenance)
        return {
            "source_id": str(recorded.source_id),
            "source_hash": recorded.source_hash,
            "source_code": source_code,
            "mode": "3d",
            "generator_kind": generator_kind,
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
            error_code=error.type or "agent_freecad_operation_generation_failed",
            error_message=str(error),
        )
        raise error from exc


async def agent_repair_operations(payload: dict[str, Any], *, freecad_operations: FreeCADOperationGenerator) -> dict[str, Any]:
    request = _agent_v2_request(payload)
    if request.revision_restore is not None:
        raise ApplicationError(
            "historical revision restoration cannot rewrite model operations",
            type="revision_restore_repair_forbidden", non_retryable=True,
        )
    if request.structured_modification is not None:
        raise ApplicationError(
            "明确提交的原生修改或参数修改未通过检查，请调整输入后重新提交。原始检查："
            + str((payload.get('failure') or {}).get('error_message') or '未提供详细错误')[:2500],
            type="native_edit_repair_forbidden", non_retryable=True,
        )
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
            type="agent_freecad_repair_not_allowed",
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
            FreeCADOperationPlan.model_validate_json(replay["source_code"])
            return {
                "source_id": str(replay["id"]),
                "source_hash": replay["source_hash"],
                "source_code": replay["source_code"],
                "repair_step_key": repair_step_key,
                "failure_class": decision.failure_class,
                "strategy": decision.strategy,
                "signature": decision.signature,
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
                "FreeCAD repair input does not belong to candidate",
                type="agent_freecad_repair_source_missing",
                non_retryable=True,
            )
        if str(source["source_hash"]) != str(payload["source_hash"]):
            raise ApplicationError(
                "FreeCAD repair input hash does not match persisted source",
                type="agent_freecad_repair_source_hash_mismatch",
                non_retryable=True,
            )
        step_id = await _start_agent_logical_step(
            connection,
            tenant_id=request.tenant_id,
            workflow_id=request.workflow_run_id,
            step_key=repair_step_key,
            step_index=int(payload["step_index"]),
            kind="agent_freecad_repair",
        )
    try:
        repaired = await _await_provider_operation(freecad_operations.repair(
            source_code=str(source["source_code"]),
            failure=failure,
            base_state=(
                dict(payload["base_state"])
                if payload.get("base_state") is not None
                else None
            ),
            output_formats=request.output_formats,
        ))
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
                generator_kind=f"repair:{decision.failure_class}:freecad_operations",
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
            if not recorded.replayed:
                await _record_freecad_inspections(connection, request, repair_step_key, provenance)
            await append_workflow_event(
                connection,
                tenant_id=request.tenant_id,
                workflow_id=request.workflow_run_id,
                event_type="agent.freecad.operations_repaired",
                payload={
                    "repair_step_key": repair_step_key,
                    "prior_source_id": str(source_id),
                    "source_id": str(recorded.source_id),
                    "source_hash": recorded.source_hash,
                    "failure_class": decision.failure_class,
                    "strategy": decision.strategy,
                    "signature": decision.signature,
                    "error_code": failure["error_code"],
                },
            )
        return {
            "source_id": str(recorded.source_id),
            "source_hash": recorded.source_hash,
            "source_code": repaired.source_code,
            "repair_step_key": repair_step_key,
            "failure_class": decision.failure_class,
            "strategy": decision.strategy,
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
            error_code=error.type or "agent_freecad_repair_failed",
            error_message=str(error),
        )
        raise error from exc
