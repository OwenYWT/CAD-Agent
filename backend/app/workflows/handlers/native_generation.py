"""Native generation use cases; Temporal names live in the adapter."""
from __future__ import annotations
import hashlib
from typing import Any
from sqlalchemy import text
from temporalio.exceptions import ApplicationError
from app.agent.durable_plan import AgentPlan
from app.agent.durable_plan import AgentPlanStep
from app.agent.durable_repair import decide_repair, RepairDecision
from app.config import settings
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
from app.workflows.validation_support import _record_freecad_inspections, record_freecad_rejection
from app.workflows.revision_inputs import _revision_restore_operation_plan
from app.workflows.errors import planning_error
from app.workflows.checkpoint_inputs import checkpoint_context, checkpoint_manifest, checkpoint_artifact
from app.workflows.constraint_evidence import load_failure_snapshot, load_repair_contract, load_profile_contract
import app.freecad.profile_replan as profile_replan
from app.freecad.constraint_patch import carry_contract, progress_fingerprint
from app.freecad.constraint_repair import SKETCH_FAILURES


def _provenance_reference(provenance):
    # Full read results live in task events. Do not copy document-sized
    # inspection traces into every Temporal model-job completion.
    return {key: provenance[key] for key in (
        'provider','model','provider_response_id','request_hash','response_hash','finish_reason','usage'
    ) if key in provenance}

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
    base_state = payload.get("base_state")
    feedback = None
    if payload.get("checkpoint_manifest_id"):
        base_state, feedback = await checkpoint_context(request, candidate_build_id,
            payload["checkpoint_manifest_id"], base_state)
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
            step_index=None if payload.get("checkpoint_enabled") else int(payload["step_index"]),
            kind="agent_freecad_operations",
        )
    try:
        if request.operation_context and request.operation_context.native_import:
            source = request.operation_context.native_import
            operation_plan = FreeCADOperationPlan.model_validate({'schema_version':'freecad-operation-plan.v1',
                'document_name':'ImportedModel', 'operations':[
                    {'op_id':f'import-inspect-{source.artifact_id.hex}', 'action':'document.inspect', 'args':{}},
                    {'op_id':f'import-export-{source.artifact_id.hex}', 'action':'document.export',
                     'args':{'formats':list(dict.fromkeys(('fcstd', *request.output_formats))), 'basename':'model'}}]})
            source_code = operation_plan.model_dump_json()
            generator_kind = 'native-file-import-v1'
            provenance = {'provider':'cad-agent','model':generator_kind,'provider_response_id':None,
                'request_hash':canonical_sha256(source.model_dump(mode='json')),
                'response_hash':hashlib.sha256(source_code.encode()).hexdigest(),'finish_reason':'deterministic','usage':{}}
        elif request.revision_restore is not None:
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
            async def rejection_sink(evidence):
                await record_freecad_rejection(request, step.step_key, evidence)
            generated = await _await_provider_operation(freecad_operations.generate(
                plan=plan,
                requirements=dict(payload["requirements"]),
                base_state=(
                    dict(base_state)
                    if base_state is not None
                    else None
                ),
                output_formats=request.output_formats,
                rejection_sink=rejection_sink,
                **({"checkpoint_enabled": True, "execution_feedback": feedback}
                   if payload.get("checkpoint_enabled") else {}),
            ))
            source_code = generated.source_code
            generator_kind = generated.generator_kind
            provenance = generated.provenance
        if feedback:
            retained = await load_repair_contract(request, source_id=feedback['source_id'], source_hash=feedback['source_hash'])
            if retained:
                checkpoint = await checkpoint_manifest(request, candidate_build_id, payload['checkpoint_manifest_id'])
                provenance = {**provenance, 'constraint_repair': carry_contract(retained,
                    FreeCADOperationPlan.model_validate_json(source_code), checkpoint_artifact(checkpoint, 'fcstd')['sha256'])}
                generator_kind += ':constraint_patch_continuation'
            retained_profile = await load_profile_contract(request, source_id=feedback['source_id'], source_hash=feedback['source_hash'])
            if retained_profile:
                checkpoint = await checkpoint_manifest(request, candidate_build_id, payload['checkpoint_manifest_id'])
                provenance = {**provenance, 'profile_replan': profile_replan.carry_contract(retained_profile,
                    FreeCADOperationPlan.model_validate_json(source_code), checkpoint_artifact(checkpoint, 'fcstd')['sha256'])}
                generator_kind += ':profile_replan_continuation'
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
                await _record_freecad_inspections(connection, request, step.step_key, provenance,
                    source_id=recorded.source_id, source_hash=recorded.source_hash)
        return {
            "source_id": str(recorded.source_id),
            "source_hash": recorded.source_hash,
            "source_code": source_code,
            "mode": "3d",
            "generator_kind": generator_kind,
            "provenance": _provenance_reference(provenance),
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
    if request.operation_context and request.operation_context.native_import:
        raise ApplicationError('导入文件未通过真实几何检查，请在原 CAD 中修正后重新导入。',
            type='native_import_repair_forbidden', non_retryable=True)
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
    diagnostic = await load_failure_snapshot(request, source_id=source_id,
        source_hash=str(payload['source_hash']), failure=failure)
    prior_contract = await load_repair_contract(request, source_id=source_id,
        source_hash=str(payload['source_hash']))
    prior_profile = await load_profile_contract(request, source_id=source_id, source_hash=str(payload['source_hash']))
    profile_context = None
    if failure['error_code'] == profile_replan.PROFILE_ERROR and payload.get('profile_replanning_v1'):
        signature = profile_replan.progress_fingerprint(diagnostic['snapshot']) if diagnostic else ''
        used = int(payload.get('profile_repair_index', repair_index)) - 1
        decision = RepairDecision(bool(diagnostic) and used < settings.profile_replan_max_attempts
            and signature not in (payload.get('seen_signatures') or ()), 'profile_geometry', 'profile_replan',
            settings.profile_replan_max_attempts, signature,
            'verified profile diagnostic is missing' if not diagnostic else
            'profile replan attempt budget exhausted' if used >= settings.profile_replan_max_attempts else
            'profile replan made no physical progress' if signature in (payload.get('seen_signatures') or ()) else
            'verified profile topology replan')
    if failure['error_code'] in SKETCH_FAILURES and diagnostic is None:
        raise ApplicationError('缺少可核验的失败草图现场，不能安全地修改约束。原始建模错误：'
            + str(failure.get('error_message') or '')[:2500],
            type='constraint_diagnostic_unavailable', non_retryable=True)
    if diagnostic and failure['error_code'] in SKETCH_FAILURES:
        signature = progress_fingerprint(diagnostic['snapshot'])
        seen = {progress_fingerprint(entry['context']['snapshot'])
                for entry in (prior_contract or {}).get('patches', [])}
        seen.update(str(item) for item in payload.get('seen_signatures') or ())
        budget = settings.constraint_repair_max_attempts
        exhausted = max(repair_index - 1, len((prior_contract or {}).get('patches', []))) >= budget
        repeated = signature in seen
        decision = RepairDecision(not exhausted and not repeated, 'sketch_constraints',
            'local_constraint_patch', budget, signature,
            'constraint repair made no physical or execution progress' if repeated else
            'constraint repair attempt budget exhausted' if exhausted else 'verified local constraint repair')
    if not decision.repairable:
        raise ApplicationError(
            decision.reason + '. Original modeling failure: ' + str(failure.get('error_message') or '')[:2500],
            {
                "failure_class": decision.failure_class,
                "strategy": decision.strategy,
                "signature": decision.signature,
                "original_error_code": failure['error_code'],
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
            step_index=None if payload.get("checkpoint_enabled") else int(payload["step_index"]),
            kind="agent_freecad_repair",
        )
    try:
        async def rejection_sink(evidence):
            await record_freecad_rejection(request, repair_step_key, evidence)
        base_state = payload.get('base_state')
        if payload.get('checkpoint_manifest_id'):
            base_state, _ = await checkpoint_context(request, candidate_build_id,
                payload['checkpoint_manifest_id'], base_state)
        if decision.strategy == 'profile_replan':
            profile_context = profile_replan.build_context(
                FreeCADOperationPlan.model_validate_json(str(source['source_code'])), diagnostic['snapshot'],
                failure, base_state, prior_contract)
            if prior_profile and prior_profile['sketch'] != profile_context['sketch']:
                raise profile_replan.rejected('another replanned sketch has outstanding verification obligations')
        repaired = await _await_provider_operation(freecad_operations.repair(
            source_code=str(source["source_code"]),
            failure=failure,
            base_state=(
                dict(base_state)
                if base_state is not None
                else None
            ),
            output_formats=request.output_formats,
            rejection_sink=rejection_sink,
            **({'diagnostic': diagnostic['snapshot'], 'prior_contract': prior_contract} if diagnostic else {}),
            **({'profile_context': profile_context} if profile_context else {}),
        ))
        provenance = repaired.provenance
        if prior_contract and not provenance.get('constraint_repair'):
            # Geometry/visual repairs after a constraint repair inherit its frozen
            # requirements. They cannot silently discard earlier proof obligations.
            provenance = {**provenance, 'constraint_repair': carry_contract(prior_contract,
                    repaired.operation_plan, prior_contract.get('execution_checkpoint_hash', prior_contract['checkpoint_hash']))}
        if prior_profile and not provenance.get('profile_replan'):
            provenance = {**provenance, 'profile_replan': profile_replan.carry_contract(prior_profile,
                repaired.operation_plan, prior_profile['execution_checkpoint_hash'])}
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
                generator_kind=f"repair:{decision.failure_class}:" + ('constraint_patch' if provenance.get('constraint_repair') else 'freecad_operations')
                    + (':profile_replan' if provenance.get('profile_replan') else ''),
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
                await _record_freecad_inspections(connection, request, repair_step_key, provenance,
                    source_id=recorded.source_id, source_hash=recorded.source_hash)
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
            "provenance": _provenance_reference(provenance),
            "replayed": recorded.replayed,
        }
    except Exception as exc:
        error = planning_error(exc)
        if failure['error_code'] == profile_replan.PROFILE_ERROR:
            error = ApplicationError('原始轮廓错误：' + str(failure.get('error_message') or '')[:1800]
                + '\n几何重规划失败：' + str(error)[:1800],
                {'original_error_code': failure['error_code'], 'operation_id': failure.get('operation_id')},
                type=error.type or 'profile_replan_failed', non_retryable=True)
        await _fail_agent_logical_step(
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            workflow_id=request.workflow_run_id,
            step_key=repair_step_key,
            error_code=error.type or "agent_freecad_repair_failed",
            error_message=str(error),
        )
        raise error from exc
