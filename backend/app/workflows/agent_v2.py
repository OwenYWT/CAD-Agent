"""Version-isolated Temporal definition for the fused durable Agent flow."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

from temporalio import workflow
from app.workflows.document_queue import wait_for_document, release_document
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.workflow import ActivityCancellationType

from app.geometry_ir.planner import build_geometry_plan, summarize_geometry_plan
from app.topology.step_resolver import resolve_step_topology
from app.validation.durable_geometry import DurableGeometryReport
from app.validation.feature_evidence import (
    build_feature_evidence,
    build_feature_repair_context,
    summarize_feature_evidence,
)
from app.validation.verification.evaluator import (
    build_verification_targets,
    evaluate_verification_targets,
    summarize_verification_evidence,
    summarize_verification_targets,
)


_CONTROL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=3,
)


def required_gate_blocks(mode: str, outcome: str) -> bool:
    """One policy rule shared by visual and DFM workflow gates."""
    if mode not in {"required", "advisory"}:
        raise ValueError(f"unsupported active gate mode: {mode}")
    if outcome not in {"passed", "failed", "indeterminate"}:
        raise ValueError(f"unsupported gate outcome: {outcome}")
    return mode == "required" and outcome != "passed"


@workflow.defn(name="McadAgentWorkflowV2")
class McadAgentWorkflowV2:
    """Persist planning first and block execution at an explicit gate."""

    def __init__(self) -> None:
        self._confirmation: dict[str, Any] | None = None
        self._cancel_reason: str | None = None
        self._phase = "pending"
        self._plan: dict[str, Any] | None = None
        self._candidate_build_id: str | None = None
        self._validation_repair_v2 = False
        self._provider_streaming_v1 = False

    @workflow.signal(name="confirmation")
    async def confirmation(self, decision: dict[str, Any]) -> None:
        if self._confirmation is None:
            self._confirmation = {
                "accepted": bool(decision.get("accepted")),
                "note": str(decision.get("note") or "")[:4000],
            }

    @workflow.signal(name="cancel_requested")
    async def cancel_requested(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = str(reason or "cancelled")[:4000]

    @workflow.query(name="phase")
    def phase(self) -> str:
        return self._phase

    @workflow.query(name="agent_plan")
    def agent_plan(self) -> dict[str, Any] | None:
        return self._plan

    async def _activity(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        suffix: str,
        execution: bool = False,
    ) -> dict[str, Any]:
        timeout_seconds = int(payload.get("timeout_seconds") or 120)
        provider_operation = self._provider_streaming_v1 and name in {
            "agent_v2.generate_operations", "agent_v2.repair_operations"
        }
        activity_task = asyncio.create_task(
            workflow.execute_activity(
                name,
                payload,
                activity_id=f"{payload['workflow_run_id']}:{suffix}",
                start_to_close_timeout=(
                    timedelta(seconds=timeout_seconds + 120)
                    if execution
                    else timedelta(minutes=20) if provider_operation else timedelta(minutes=5)
                ),
                heartbeat_timeout=(timedelta(seconds=15) if execution or provider_operation else None),
                retry_policy=_CONTROL_RETRY,
                cancellation_type=(
                    ActivityCancellationType.TRY_CANCEL if provider_operation
                    else ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
                ),
                result_type=dict,
            )
        )
        if not execution and not provider_operation:
            return await activity_task
        cancel_wait = asyncio.create_task(
            workflow.wait_condition(lambda: self._cancel_reason is not None)
        )
        done, _ = await workflow.wait(
            {activity_task, cancel_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_wait in done and not activity_task.done():
            activity_task.cancel()
            try:
                await activity_task
            except (asyncio.CancelledError, Exception):
                pass
            raise asyncio.CancelledError
        cancel_wait.cancel()
        try:
            await cancel_wait
        except asyncio.CancelledError:
            pass
        result = await activity_task
        if self._cancel_reason is not None:
            raise asyncio.CancelledError
        return result

    async def _record_cancel(self, request: dict[str, Any]) -> dict[str, Any]:
        self._phase = "cancelling"
        if self._candidate_build_id is not None:
            await self._activity(
                "agent_v2.terminate_candidate",
                {
                    **request,
                    "candidate_build_id": self._candidate_build_id,
                    "target_status": "cancelled",
                },
                suffix="cancel-candidate",
            )
        result = await self._activity(
            "mcad.record_cancel",
            {
                **request,
                "reason": self._cancel_reason or "用户取消",
            },
            suffix="record-cancel",
        )
        self._phase = "cancelled"
        return result

    @staticmethod
    def _execution_failure(exc: Exception) -> dict[str, Any] | None:
        application_error = (
            exc.cause
            if isinstance(exc, ActivityError)
            and isinstance(exc.cause, ApplicationError)
            else exc
            if isinstance(exc, ApplicationError)
            else None
        )
        if application_error is None:
            return None
        detail = next(
            (
                item
                for item in application_error.details
                if isinstance(item, dict)
            ),
            {},
        )
        return {
            "execution_attempt_id": detail.get("execution_attempt_id"),
            "category": str(detail.get("category") or "internal"),
            "error_code": str(
                detail.get("error_code")
                or application_error.type
                or "agent_model_execution_failed"
            ),
            "error_message": str(
                detail.get("error_message") or application_error
            )[:4000],
            "runtime_error_type": detail.get("runtime_error_type"),
        }

    @staticmethod
    def _design_repair_context(plan: dict[str, Any]) -> str:
        dimensions = [
            f"{item['name']}={item['value']} {item.get('unit') or 'mm'}"
            for item in plan["design_brief"].get("critical_dimensions") or ()
            if item.get("value") is not None
        ]
        geometry_summary = summarize_geometry_plan(plan)
        verification_targets = build_verification_targets(plan)
        return (
            f"Design objective: {plan['objective']}. "
            f"Required dimensions: {', '.join(dimensions) or 'not specified'}."
            + " Preserve every requested feature, count, position and depth. "
            + "Acceptance criteria: " + json.dumps(plan["design_brief"].get("acceptance_criteria", []), ensure_ascii=False)
            + f" Geometry IR: {geometry_summary}."
            + f" Verification targets: {', '.join(target.target_id for target in verification_targets) or 'none'}."
        )

    async def _model_step(
        self,
        *,
        request: dict[str, Any],
        plan: dict[str, Any],
        candidate_build_id: str,
        step: dict[str, Any],
        step_index: int,
        plan_step_index: int,
        requirements: dict[str, Any],
        previous_source: str | None = None,
        predecessor_source_id: str | None = None,
        input_source_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        generated = await self._activity(
            "agent_v2.generate_source",
            {
                **request,
                "candidate_build_id": candidate_build_id,
                "plan": plan,
                "step": step,
                "step_index": step_index,
                "plan_step_index": plan_step_index,
                "requirements": requirements,
                "previous_source": previous_source,
                "predecessor_source_id": predecessor_source_id,
                "input_source_ids": list(input_source_ids),
            },
            suffix=f"generate-source-{step['step_key']}",
        )
        run_step_key = str(step["step_key"])
        run_step_kind = "agent_model"
        run_step_index = step_index
        repair_count = 0
        seen_signatures: list[str] = []
        while True:
            try:
                executed = await self._activity(
                    "agent_v2.execute_model",
                    {
                        **request,
                        "candidate_build_id": candidate_build_id,
                        "plan": plan,
                        "step": step,
                        "step_index": run_step_index,
                        "run_step_key": run_step_key,
                        "run_step_kind": run_step_kind,
                        "source_id": generated["source_id"],
                        "source_hash": generated["source_hash"],
                        "source_code": generated["source_code"],
                        "mode": generated["mode"],
                        "timeout_seconds": 120,
                    },
                    suffix=(
                        f"execute-model-{step['step_key']}"
                        if repair_count == 0
                        else f"execute-repair-{step['step_key']}-{repair_count:02d}"
                    ),
                    execution=True,
                )
                break
            except Exception as exc:
                failure = self._execution_failure(exc)
                if failure is None or failure["category"] not in {
                    "user_code",
                    "cad_kernel",
                    "validation",
                }:
                    raise
                if repair_count >= 2:
                    raise
                failure["error_message"] = (
                    f"{failure['error_message']}\n"
                    f"{self._design_repair_context(plan)}"
                )
                repair_index = repair_count + 1
                repair_step_index = 10_000 + plan_step_index * 10 + repair_index
                repaired = await self._activity(
                    "agent_v2.repair_source",
                    {
                        **request,
                        "candidate_build_id": candidate_build_id,
                        "source_id": generated["source_id"],
                        "source_hash": generated["source_hash"],
                        "failure": failure,
                        "repair_index": repair_index,
                        "original_step_key": step["step_key"],
                        "step_index": repair_step_index,
                        "seen_signatures": seen_signatures,
                    },
                    suffix=f"repair-source-{step['step_key']}-{repair_index:02d}",
                )
                seen_signatures.append(str(repaired["signature"]))
                generated = {**generated, **repaired}
                run_step_key = str(repaired["repair_step_key"])
                run_step_kind = "agent_repair"
                run_step_index = repair_step_index
                repair_count = repair_index
        return {
            "step": step,
            "generated": generated,
            "executed": executed,
            "repair_count": repair_count,
            "seen_signatures": seen_signatures,
            "mode": generated["mode"],
        }

    async def _freecad_model_step(
        self,
        *,
        request: dict[str, Any],
        plan: dict[str, Any],
        candidate_build_id: str,
        step: dict[str, Any],
        step_index: int,
        requirements: dict[str, Any],
        base_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        generated = await self._activity(
            "agent_v2.generate_operations",
            {
                **request,
                "candidate_build_id": candidate_build_id,
                "plan": plan,
                "step": step,
                "step_index": step_index,
                "requirements": requirements,
                "base_state": base_state,
            },
            suffix=f"generate-operations-{step['step_key']}",
        )
        repair_count = 0
        seen_signatures: list[str] = []
        run_step_key = str(step["step_key"])
        run_step_kind = "agent_freecad_operations"
        run_step_index = step_index
        while True:
            try:
                executed = await self._activity(
                    "agent_v2.execute_freecad",
                    {
                        **request,
                        "candidate_build_id": candidate_build_id,
                        "plan": plan,
                        "step": step,
                        "step_index": run_step_index,
                        "run_step_key": run_step_key,
                        "run_step_kind": run_step_kind,
                        "source_id": generated["source_id"],
                        "source_hash": generated["source_hash"],
                        "source_code": generated["source_code"],
                        "mode": "3d",
                        "timeout_seconds": 180,
                    },
                    suffix=(
                        f"execute-freecad-{step['step_key']}"
                        if repair_count == 0
                        else f"execute-freecad-repair-{step['step_key']}-{repair_count:02d}"
                    ),
                    execution=True,
                )
                break
            except Exception as exc:
                if request.get("revision_restore") is not None:
                    raise
                failure = self._execution_failure(exc)
                if failure is None or failure["category"] not in {
                    "user_code",
                    "cad_kernel",
                    "validation",
                }:
                    raise
                if repair_count >= 2:
                    raise
                failure["error_message"] = (
                    f"{failure['error_message']}\n"
                    f"{self._design_repair_context(plan)}"
                )
                repair_index = repair_count + 1
                repair_step_index = 10_000 + repair_index
                repaired = await self._activity(
                    "agent_v2.repair_operations",
                    {
                        **request,
                        "candidate_build_id": candidate_build_id,
                        "source_id": generated["source_id"],
                        "source_hash": generated["source_hash"],
                        "failure": failure,
                        "repair_index": repair_index,
                        "original_step_key": step["step_key"],
                        "step_index": repair_step_index,
                        "seen_signatures": seen_signatures,
                        "base_state": base_state,
                    },
                    suffix=f"repair-operations-{step['step_key']}-{repair_index:02d}",
                )
                seen_signatures.append(str(repaired["signature"]))
                generated = {**generated, **repaired}
                run_step_key = str(repaired["repair_step_key"])
                run_step_kind = "agent_freecad_repair"
                run_step_index = repair_step_index
                repair_count = repair_index
        return {
            "step": step,
            "generated": generated,
            "executed": executed,
            "repair_count": repair_count,
            "seen_signatures": seen_signatures,
            "mode": "3d",
            "base_state": base_state,
        }

    async def _geometry_gate(
        self,
        *,
        request: dict[str, Any],
        plan: dict[str, Any],
        candidate_build_id: str,
        modeled: dict[str, Any],
        plan_step_index: int,
        expected_dimensions: dict[str, float],
        validation_cycle: int = 0,
    ) -> dict[str, Any]:
        geometry_policy = plan["validation_policy"]["geometry"]
        if geometry_policy["mode"] != "required":
            raise ApplicationError(
                "Durable Agent V2 requires the geometry gate.",
                type="agent_geometry_policy_invalid",
                non_retryable=True,
            )
        step = modeled["step"]
        generated = dict(modeled["generated"])
        executed = dict(modeled["executed"])
        repair_count = int(modeled.get("repair_count") or 0)
        seen_signatures = list(modeled.get("seen_signatures") or [])
        geometry_budget = int(geometry_policy["repair_budget"])
        validation_index = 0
        while True:
            validation_index += 1
            validation_step_key = (
                f"geometry-{step['step_key']}-{validation_index:02d}"
                if validation_cycle == 0
                else (
                    f"geometry-{step['step_key']}-v{validation_cycle:02d}-"
                    f"{validation_index:02d}"
                )
            )
            geometry = await self._activity(
                "agent_v2.validate_geometry",
                {
                    **request,
                    "candidate_build_id": candidate_build_id,
                    "staging_manifest_id": executed["staging_manifest_id"],
                    "validation_step_key": validation_step_key,
                    "step_index": (
                        20_000
                        + plan_step_index * 100
                        + validation_cycle * 10
                        + validation_index
                    ),
                    "expected_dimensions_mm": expected_dimensions,
                    "dimension_tolerance": 0.05,
                    "timeout_seconds": 120,
                },
                suffix=f"validate-{validation_step_key}",
                execution=True,
            )
            geometry_report_payload = dict(geometry["report"])
            geometry_report_payload.pop("runtime_provenance", None)
            geometry_report = DurableGeometryReport.model_validate(
                geometry_report_payload
            )
            geometry_plan = build_geometry_plan(plan)
            verification_targets = build_verification_targets(plan)
            verification_evidence = evaluate_verification_targets(
                verification_targets,
                geometry_report,
                evidence_ref=str(geometry["evidence_id"]),
            )
            topology_resolutions = tuple(
                resolve_step_topology(
                    target,
                    feature_id=target.feature_id,
                    evidence_ref=str(geometry["evidence_id"]),
                    backend_object_id=str(executed["staging_manifest_id"]),
                )
                for target in verification_targets
            )
            feature_result = build_feature_evidence(
                step=step,
                generated=generated,
                executed=executed,
                geometry_report=geometry_report,
                verification_evidence=verification_evidence,
                topology_resolutions=topology_resolutions,
                geometry_plan=geometry_plan,
            )
            geometry = {
                **geometry,
                "report": geometry_report.model_dump(mode="json"),
                "geometry_plan": geometry_plan.model_dump(mode="json"),
                "verification_targets": [
                    item.model_dump(mode="json") for item in verification_targets
                ],
                "verification_evidence": [
                    item.model_dump(mode="json") for item in verification_evidence
                ],
                "topology_resolutions": [
                    item.model_dump(mode="json") for item in topology_resolutions
                ],
                "feature_evidence": feature_result.evidence.model_dump(
                    mode="json"
                ),
            }
            if geometry["outcome"] == "passed":
                return {
                    **modeled,
                    "generated": generated,
                    "executed": executed,
                    "geometry": geometry,
                    "repair_count": repair_count,
                    "seen_signatures": seen_signatures,
                }
            if geometry["outcome"] != "failed" or repair_count >= geometry_budget:
                raise ApplicationError(
                    "Required geometry validation did not pass.",
                    {
                        "outcome": geometry["outcome"],
                        "evidence_id": geometry["evidence_id"],
                        "staging_manifest_id": executed["staging_manifest_id"],
                    },
                    type="agent_geometry_validation_failed",
                    non_retryable=True,
                )
            repair_index = repair_count + 1
            failure = {
                "execution_attempt_id": geometry.get("attempt_id"),
                "category": "validation",
                "error_code": "geometry_validation_failed",
                "error_message": (
                    "Geometry validation failed. Issues: "
                    + (
                        "; ".join(geometry["report"].get("issues") or ())
                        or "unspecified"
                    )
                    + ". Expected dimensions: "
                    + str(geometry["report"].get("expected_dimensions_mm") or {})
                    + ". Measured artifacts: "
                    + str(
                        [
                            {
                                "format": item.get("format"),
                                "dimensions_mm": item.get("dimensions_mm"),
                                "issues": item.get("issues"),
                            }
                            for item in geometry["report"].get("artifacts") or ()
                        ]
                    )
                    + ". "
                    + self._design_repair_context(plan)
                    + ". Verification targets: "
                    + summarize_verification_targets(verification_targets)
                    + ". Verification evidence: "
                    + summarize_verification_evidence(verification_evidence)
                    + ". Feature evidence: "
                    + summarize_feature_evidence(feature_result.evidence)
                ),
                "runtime_error_type": "GeometryError",
                "repair_context": build_feature_repair_context(
                    geometry_plan=geometry_plan,
                    verification_targets=verification_targets,
                    verification_evidence=verification_evidence,
                    topology_resolutions=topology_resolutions,
                    feature_evidence=feature_result.evidence,
                    geometry_report=geometry_report,
                    expected_dimensions_mm=expected_dimensions,
                ),
            }
            repair_step_index = 10_000 + plan_step_index * 10 + repair_index
            freecad = plan.get("modeling_backend") == "freecad" or (
                plan.get("modeling_backend") is None
                and request.get("modeling_backend") == "freecad"
            )
            repaired = await self._activity(
                (
                    "agent_v2.repair_operations"
                    if freecad
                    else "agent_v2.repair_source"
                ),
                {
                    **request,
                    "candidate_build_id": candidate_build_id,
                    "source_id": generated["source_id"],
                    "source_hash": generated["source_hash"],
                    "failure": failure,
                    "repair_index": repair_index,
                    "original_step_key": step["step_key"],
                    "step_index": repair_step_index,
                    "seen_signatures": seen_signatures,
                    "base_state": modeled.get("base_state"),
                },
                suffix=f"repair-geometry-{step['step_key']}-{repair_index:02d}",
            )
            seen_signatures.append(str(repaired["signature"]))
            generated = {**generated, **repaired}
            repair_count = repair_index
            executed = await self._activity(
                (
                    "agent_v2.execute_freecad"
                    if freecad
                    else "agent_v2.execute_model"
                ),
                {
                    **request,
                    "candidate_build_id": candidate_build_id,
                    "plan": plan,
                    "step": step,
                    "step_index": repair_step_index,
                    "run_step_key": repaired["repair_step_key"],
                    "run_step_kind": (
                        "agent_freecad_repair" if freecad else "agent_repair"
                    ),
                    "source_id": generated["source_id"],
                    "source_hash": generated["source_hash"],
                    "source_code": generated["source_code"],
                    "mode": modeled["mode"],
                    "timeout_seconds": 120,
                    "supersedes_staging_manifest_id": executed[
                        "staging_manifest_id"
                    ],
                },
                suffix=f"execute-geometry-repair-{step['step_key']}-{repair_index:02d}",
                execution=True,
            )

    async def _model_step_outcome(self, **kwargs: Any) -> dict[str, Any]:
        try:
            return {"result": await self._model_step(**kwargs), "error": None}
        except Exception as exc:
            return {"result": None, "error": exc}

    async def _visual_gate(
        self,
        *,
        request: dict[str, Any],
        plan: dict[str, Any],
        candidate_build_id: str,
        modeled: dict[str, Any],
        plan_step_index: int,
        expected_dimensions: dict[str, float],
        validation_cycle: int = 0,
    ) -> dict[str, Any]:
        policy = plan["validation_policy"]["visual"]
        if policy["mode"] == "disabled":
            return {**modeled, "visual": None}
        current = modeled
        visual_repairs = 0
        cycle_key = f"-c{validation_cycle:02d}" if validation_cycle else ""
        while True:
            render_index = visual_repairs + 1
            rendered = await self._activity(
                "agent_v2.render_visual",
                {
                    **request,
                    "candidate_build_id": candidate_build_id,
                    "staging_manifest_id": current["executed"]["staging_manifest_id"],
                    "validation_step_key": (
                        f"visual-{current['step']['step_key']}{cycle_key}-{render_index:02d}"
                    ),
                    "step_index": 30_000 + plan_step_index * 10 + validation_cycle * 100 + render_index,
                    "gate_mode": policy["mode"],
                    "timeout_seconds": 120,
                },
                suffix=(
                    f"render-visual-{current['step']['step_key']}{cycle_key}-{render_index:02d}"
                ),
                execution=True,
            )
            if rendered.get("outcome") == "indeterminate":
                visual = rendered
            else:
                visual = await self._activity(
                    "agent_v2.judge_visual",
                    {
                        **request,
                        "candidate_build_id": candidate_build_id,
                        "staging_manifest_id": current["executed"][
                            "staging_manifest_id"
                        ],
                        "render_attempt_id": rendered["attempt_id"],
                        "render_step_id": rendered["step_id"],
                        "renders": rendered["renders"],
                        "runtime_provenance": rendered["runtime_provenance"],
                        "objective": plan["objective"],
                        "design_brief": plan["design_brief"],
                        "gate_mode": policy["mode"],
                    },
                    suffix=(
                        f"judge-visual-{current['step']['step_key']}{cycle_key}-{render_index:02d}"
                    ),
                )
            if visual["outcome"] == "passed":
                return {**current, "visual": visual}
            if visual["outcome"] == "indeterminate":
                if required_gate_blocks(policy["mode"], visual["outcome"]):
                    raise ApplicationError(
                        "Required visual validation is indeterminate.",
                        {
                            "outcome": "indeterminate",
                            "evidence_id": visual["evidence_id"],
                        },
                        type="agent_visual_validation_indeterminate",
                        non_retryable=True,
                    )
                return {**current, "visual": visual}
            if visual_repairs >= int(policy["repair_budget"]):
                if required_gate_blocks(policy["mode"], visual["outcome"]) or self._validation_repair_v2:
                    raise ApplicationError(
                        "Required visual validation failed.",
                        {
                            "outcome": "failed",
                            "evidence_id": visual["evidence_id"],
                            "repair_count": visual_repairs,
                            "repair_budget": policy["repair_budget"],
                        },
                        type="agent_visual_validation_failed",
                        non_retryable=True,
                    )
                return {**current, "visual": visual}
            freecad = plan.get("modeling_backend") == "freecad" or (
                plan.get("modeling_backend") is None
                and request.get("modeling_backend") == "freecad"
            )
            if freecad and policy["mode"] == "advisory" and not self._validation_repair_v2:
                # The required geometry gate has already passed. Persist the real
                # visual mismatch as advisory evidence, but do not risk replacing
                # a valid typed FreeCAD plan merely to satisfy a non-blocking gate.
                return {**current, "visual": visual}
            visual_repairs += 1
            repair_step_key = (
                f"visual-repair-{current['step']['step_key']}{cycle_key}-{visual_repairs:02d}"
            )
            judgment = dict(visual["report"].get("judgment") or {})
            repair_index = int(current.get("repair_count") or 0) + 1
            if freecad:
                repaired = await self._activity(
                    "agent_v2.repair_operations",
                    {
                        **request,
                        "candidate_build_id": candidate_build_id,
                        "source_id": current["generated"]["source_id"],
                        "source_hash": current["generated"]["source_hash"],
                        "repair_step_key": repair_step_key,
                        "step_index": 40_000 + plan_step_index * 10 + validation_cycle * 100 + visual_repairs,
                        "repair_index": repair_index,
                        "gate_repair_index": visual_repairs,
                        "original_step_key": current["step"]["step_key"],
                        "seen_signatures": current.get("seen_signatures") or (),
                        "base_state": current.get("base_state"),
                        "failure": {
                            "execution_attempt_id": rendered.get("attempt_id"),
                            "category": "validation",
                            "error_code": "visual_validation_failed",
                            "error_message": (
                                "Visual validation issues: "
                                + "; ".join(judgment.get("issues") or ())
                                + ". Suggestions: "
                                + "; ".join(judgment.get("suggestions") or ())
                                + ". " + self._design_repair_context(plan)
                            ),
                            "runtime_error_type": "VisualError",
                        },
                    },
                    suffix=repair_step_key,
                )
            else:
                repaired = await self._activity(
                    "agent_v2.repair_visual",
                    {
                        **request,
                        "candidate_build_id": candidate_build_id,
                        "source_id": current["generated"]["source_id"],
                        "source_hash": current["generated"]["source_hash"],
                        "repair_step_key": repair_step_key,
                        "step_index": 40_000 + plan_step_index * 10 + visual_repairs,
                        "issues": judgment.get("issues") or (),
                        "suggestions": judgment.get("suggestions") or (),
                    },
                    suffix=repair_step_key,
                )
            generated = {**current["generated"], **repaired}
            executed = await self._activity(
                (
                    "agent_v2.execute_freecad"
                    if freecad
                    else "agent_v2.execute_model"
                ),
                {
                    **request,
                    "candidate_build_id": candidate_build_id,
                    "plan": plan,
                    "step": current["step"],
                    "step_index": 40_000 + plan_step_index * 10 + validation_cycle * 100 + visual_repairs,
                    "run_step_key": repair_step_key,
                    "run_step_kind": (
                        "agent_freecad_repair"
                        if freecad
                        else "agent_visual_repair"
                    ),
                    "source_id": generated["source_id"],
                    "source_hash": generated["source_hash"],
                    "source_code": generated["source_code"],
                    "mode": current["mode"],
                    "timeout_seconds": 120,
                    "supersedes_staging_manifest_id": current["executed"][
                        "staging_manifest_id"
                    ],
                },
                suffix=f"execute-{repair_step_key}",
                execution=True,
            )
            current = await self._geometry_gate(
                request=request,
                plan=plan,
                candidate_build_id=candidate_build_id,
                modeled={
                    **current,
                    "generated": generated,
                    "executed": executed,
                    "repair_count": repair_index,
                    "seen_signatures": [*current.get("seen_signatures", []), *([repaired["signature"]] if repaired.get("signature") else [])],
                },
                plan_step_index=plan_step_index,
                expected_dimensions=expected_dimensions,
                validation_cycle=validation_cycle * 10 + visual_repairs,
            )

    async def _dfm_gate(
        self,
        *,
        request: dict[str, Any],
        plan: dict[str, Any],
        candidate_build_id: str,
        modeled: dict[str, Any],
        plan_step_index: int,
        expected_dimensions: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        if self._validation_repair_v2:
            return await self._dfm_gate_with_repairs(request=request, plan=plan,
                candidate_build_id=candidate_build_id, modeled=modeled,
                plan_step_index=plan_step_index, expected_dimensions=expected_dimensions or {})
        policy = plan["validation_policy"]["dfm"]
        if policy["mode"] == "disabled":
            return {**modeled, "dfm": None}
        dfm = await self._activity(
            "agent_v2.validate_dfm",
            {
                **request,
                "candidate_build_id": candidate_build_id,
                "staging_manifest_id": modeled["executed"]["staging_manifest_id"],
                "validation_step_key": f"dfm-{modeled['step']['step_key']}",
                "step_index": 50_000 + plan_step_index,
                "gate_mode": policy["mode"],
                "timeout_seconds": 120,
            },
            suffix=f"validate-dfm-{modeled['step']['step_key']}",
            execution=True,
        )
        if required_gate_blocks(policy["mode"], dfm["outcome"]):
            raise ApplicationError(
                "Required DFM validation did not pass.",
                {
                    "outcome": dfm["outcome"],
                    "evidence_id": dfm["evidence_id"],
                },
                type="agent_dfm_validation_failed",
                non_retryable=True,
            )
        return {**modeled, "dfm": dfm}

    async def _dfm_gate_with_repairs(self, *, request, plan, candidate_build_id,
                                   modeled, plan_step_index, expected_dimensions):
        policy = plan["validation_policy"]["dfm"]
        if policy["mode"] == "disabled":
            return {**modeled, "dfm": None}
        current = modeled
        repair_count = 0
        freecad = plan.get("modeling_backend") == "freecad" or (
            plan.get("modeling_backend") is None and request.get("modeling_backend") == "freecad")
        while True:
            key = f"dfm-{current['step']['step_key']}-{repair_count:02d}"
            dfm = await self._activity("agent_v2.validate_dfm", {
                **request, "candidate_build_id": candidate_build_id,
                "staging_manifest_id": current["executed"]["staging_manifest_id"],
                "validation_step_key": key, "step_index": 50_000 + plan_step_index * 100 + repair_count,
                "gate_mode": policy["mode"], "timeout_seconds": 120,
            }, suffix=f"validate-{key}", execution=True)
            if dfm["outcome"] == "passed":
                return {**current, "dfm": dfm}
            if dfm["outcome"] != "failed" or repair_count >= int(policy["repair_budget"]):
                if required_gate_blocks(policy["mode"], dfm["outcome"]):
                    raise ApplicationError("DFM validation did not pass within its repair budget.",
                        {"outcome": dfm["outcome"], "evidence_id": dfm["evidence_id"],
                         "repair_count": repair_count, "repair_budget": policy["repair_budget"]},
                        type="agent_dfm_validation_failed", non_retryable=True)
                return {**current, "dfm": dfm}
            repair_count += 1
            repair_index = int(current.get("repair_count", 0)) + 1
            repair_key = f"dfm-repair-{current['step']['step_key']}-{repair_count:02d}"
            step_index = 60_000 + plan_step_index * 100 + repair_count
            report = dfm["report"]
            failure = {"execution_attempt_id": dfm.get("attempt_id"), "category": "validation",
                "error_code": "dfm_validation_failed", "runtime_error_type": "DFMError",
                "error_message": "Measured DFM findings: " + json.dumps({
                    "metrics": report.get("metrics", {}), "violations": report.get("violations", [])[:32],
                    "issues": report.get("issues", [])[:32]}, ensure_ascii=False)[:16000]
                    + ". " + self._design_repair_context(plan)}
            repaired = await self._activity("agent_v2.repair_operations" if freecad else "agent_v2.repair_source", {
                **request, "candidate_build_id": candidate_build_id,
                "source_id": current["generated"]["source_id"], "source_hash": current["generated"]["source_hash"],
                "repair_step_key": repair_key, "step_index": step_index, "repair_index": repair_index,
                "gate_repair_index": repair_count, "original_step_key": current["step"]["step_key"],
                "seen_signatures": current.get("seen_signatures", []), "base_state": current.get("base_state"),
                "failure": failure,
            }, suffix=repair_key)
            generated = {**current["generated"], **repaired}
            executed = await self._activity("agent_v2.execute_freecad" if freecad else "agent_v2.execute_model", {
                **request, "candidate_build_id": candidate_build_id, "plan": plan, "step": current["step"],
                "step_index": step_index, "run_step_key": repair_key,
                "run_step_kind": "agent_freecad_repair" if freecad else "agent_repair",
                "source_id": generated["source_id"], "source_hash": generated["source_hash"],
                "source_code": generated["source_code"], "mode": current["mode"], "timeout_seconds": 180,
                "supersedes_staging_manifest_id": current["executed"]["staging_manifest_id"],
            }, suffix=f"execute-{repair_key}", execution=True)
            # Evidence for the superseded artifact must never validate new bytes.
            current = {k: v for k, v in current.items() if k not in {"geometry", "visual", "dfm"}}
            current.update(generated=generated, executed=executed, repair_count=repair_index,
                seen_signatures=[*current.get("seen_signatures", []), repaired["signature"]])
            current = await self._geometry_gate(request=request, plan=plan, candidate_build_id=candidate_build_id,
                modeled=current, plan_step_index=plan_step_index, expected_dimensions=expected_dimensions,
                validation_cycle=10 + repair_count)
            current = await self._visual_gate(request=request, plan=plan, candidate_build_id=candidate_build_id,
                modeled=current, plan_step_index=plan_step_index, expected_dimensions=expected_dimensions,
                validation_cycle=10 + repair_count)

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            if request.get("document_queue"):
                await wait_for_document(self, request)
            backend_policy_v1 = workflow.patched(
                "agent-v2-backend-policy-v1"
            )
            native_bom_v1 = workflow.patched("agent-v2-native-bom-v1")
            self._validation_repair_v2 = workflow.patched("agent-v2-validation-repair-v2")
            self._provider_streaming_v1 = workflow.patched("agent-v2-provider-streaming-v1")
            self._phase = "requirements"
            requirements = await self._activity(
                "agent_v2.requirements",
                request,
                suffix="requirements",
            )
            if self._cancel_reason is not None:
                return await self._record_cancel(request)

            decomposition: dict[str, Any] | None = None
            if request["operation"] == "generate":
                self._phase = "decomposition"
                decomposition = await self._activity(
                    "agent_v2.decompose",
                    {
                        **request,
                        "requirements": requirements["requirements"],
                        "backend_policy_v1": backend_policy_v1,
                    },
                    suffix="decompose",
                )
                if self._cancel_reason is not None:
                    return await self._record_cancel(request)

            self._phase = "planning"
            planned = await self._activity(
                "agent_v2.plan",
                {
                    **request,
                    "requirements": requirements["requirements"],
                    "decomposition": (
                        decomposition["decomposition"]
                        if decomposition is not None
                        else None
                    ),
                    "backend_policy_v1": backend_policy_v1,
                    "validation_repair_v2": self._validation_repair_v2,
                },
                suffix="plan",
            )
            self._plan = dict(planned["plan"])

            if planned["requires_confirmation"]:
                self._phase = "waiting_confirmation"
                await self._activity(
                    "mcad.wait_confirmation",
                    request,
                    suffix="wait-confirmation",
                )
                try:
                    await workflow.wait_condition(
                        lambda: (
                            self._confirmation is not None
                            or self._cancel_reason is not None
                        ),
                        timeout=timedelta(
                            seconds=int(request["confirmation_timeout_seconds"])
                        ),
                        timeout_summary="Durable Agent plan confirmation deadline",
                    )
                except asyncio.TimeoutError:
                    self._phase = "timed_out"
                    return await self._activity(
                        "mcad.record_timeout",
                        request,
                        suffix="confirmation-timeout",
                    )
                if self._cancel_reason is not None:
                    return await self._record_cancel(request)
                if not bool(
                    self._confirmation and self._confirmation["accepted"]
                ):
                    self._cancel_reason = (
                        str(self._confirmation.get("note") or "用户拒绝执行计划")
                        if self._confirmation
                        else "用户拒绝执行计划"
                    )
                    return await self._record_cancel(request)
                await self._activity(
                    "mcad.resume_after_confirmation",
                    {
                        **request,
                        "note": str(self._confirmation.get("note") or ""),
                    },
                    suffix="resume-confirmation",
                )

            self._phase = "allocating_candidate"
            candidate = await self._activity(
                "agent_v2.allocate_candidate",
                {
                    **request,
                    "plan": self._plan,
                },
                suffix="allocate-candidate",
            )
            self._candidate_build_id = str(candidate["candidate_build_id"])
            self._phase = "modeling"
            previous_source: str | None = None
            predecessor_source_id: str | None = None
            manifests: list[dict[str, Any]] = []
            use_freecad = (
                self._plan.get("modeling_backend") == "freecad"
                if backend_policy_v1
                else (
                    request.get("modeling_backend") == "freecad"
                    and self._plan["model_kind"]
                    not in {"assembly", "profile_2d"}
                )
            )
            if (
                not backend_policy_v1
                and request.get("modeling_backend") == "freecad"
                and not use_freecad
            ):
                # The approved MVP excludes Assembly and 2D-profile authoring.
                # Preserve their already-working source-code path.
                request = {**request, "modeling_backend": "cadquery"}
            requirements_payload = requirements["requirements"]
            if request["operation"] == "modify" and not use_freecad:
                requirements_payload = {
                    "existing_code": request.get("existing_code") or "",
                    "modification_plan": requirements["requirements"],
                }
            modeling_offset = 3 if request["operation"] == "generate" else 2
            modeled: list[dict[str, Any]] = []
            if use_freecad:
                plan_step_index, step = next(
                    (index, item)
                    for index, item in reversed(
                        tuple(enumerate(self._plan["steps"]))
                    )
                    if item.get("output_formats")
                )
                self._phase = f"executing:freecad:{step['step_key']}"
                modeled.append(
                    await self._freecad_model_step(
                        request=request,
                        plan=self._plan,
                        candidate_build_id=self._candidate_build_id,
                        step=step,
                        step_index=modeling_offset + plan_step_index,
                        requirements=dict(requirements_payload),
                        base_state=(
                            dict(requirements["base_state"])
                            if requirements.get("base_state") is not None
                            else None
                        ),
                    )
                )
            elif self._plan["model_kind"] == "assembly":
                part_entries = [
                    (index, step)
                    for index, step in enumerate(self._plan["steps"])
                    if step["kind"] == "assembly_part"
                ]
                for offset in range(0, len(part_entries), 3):
                    self._phase = "executing:assembly-parts"
                    batch = part_entries[offset : offset + 3]
                    batch_outcomes = await asyncio.gather(
                        *(
                            self._model_step_outcome(
                                request=request,
                                plan=self._plan,
                                candidate_build_id=self._candidate_build_id,
                                step=step,
                                step_index=modeling_offset + index,
                                plan_step_index=index,
                                requirements=requirements_payload,
                            )
                            for index, step in batch
                        )
                    )
                    modeled.extend(
                        outcome["result"]
                        for outcome in batch_outcomes
                        if outcome["result"] is not None
                    )
                    if self._cancel_reason is not None:
                        return await self._record_cancel(request)
                    failed = next(
                        (
                            outcome["error"]
                            for outcome in batch_outcomes
                            if outcome["error"] is not None
                        ),
                        None,
                    )
                    if failed is not None:
                        raise failed
                combine_index, combine = next(
                    (index, step)
                    for index, step in enumerate(self._plan["steps"])
                    if step["kind"] == "assembly_combine"
                )
                part_sources = [
                    {
                        "step_key": item["step"]["step_key"],
                        "function_name": item["step"]["step_key"].replace(
                            "-", "_"
                        ),
                        "part_name": item["step"]["part_name"],
                        "position": item["step"]["part_position"],
                        "color": item["step"]["part_color"],
                        "source_code": item["generated"]["source_code"],
                        "source_id": item["generated"]["source_id"],
                    }
                    for item in modeled
                ]
                self._phase = "executing:assembly-combine"
                modeled.append(
                    await self._model_step(
                        request=request,
                        plan=self._plan,
                        candidate_build_id=self._candidate_build_id,
                        step=combine,
                        step_index=modeling_offset + combine_index,
                        plan_step_index=combine_index,
                        requirements={"part_sources": part_sources},
                        input_source_ids=tuple(
                            item["generated"]["source_id"] for item in modeled
                        ),
                    )
                )
            else:
                for plan_step_index, step in enumerate(self._plan["steps"]):
                    if self._cancel_reason is not None:
                        return await self._record_cancel(request)
                    self._phase = f"executing:{step['step_key']}"
                    result = await self._model_step(
                        request=request,
                        plan=self._plan,
                        candidate_build_id=self._candidate_build_id,
                        step=step,
                        step_index=modeling_offset + plan_step_index,
                        plan_step_index=plan_step_index,
                        requirements=requirements_payload,
                        previous_source=previous_source,
                        predecessor_source_id=predecessor_source_id,
                    )
                    modeled.append(result)
                    previous_source = str(result["generated"]["source_code"])
                    predecessor_source_id = str(result["generated"]["source_id"])
            for item in modeled:
                step = item["step"]
                generated = item["generated"]
                executed = item["executed"]
                manifests.append(
                    {
                        "step_key": step["step_key"],
                        "source_id": generated["source_id"],
                        "source_hash": generated["source_hash"],
                        "staging_manifest_id": executed["staging_manifest_id"],
                        "manifest_hash": executed["manifest_hash"],
                    }
                )
            self._phase = "validating:geometry"
            expected_dimensions = {
                str(key): float(value)
                for key, value in dict(
                    requirements_payload.get("dimensions") or {}
                ).items()
            }
            validated: list[dict[str, Any]] = []
            for plan_step_index, item in enumerate(modeled):
                if not item["step"].get("output_formats"):
                    continue
                validated.append(
                    await self._geometry_gate(
                        request=request,
                        plan=self._plan,
                        candidate_build_id=self._candidate_build_id,
                        modeled=item,
                        plan_step_index=plan_step_index,
                        expected_dimensions=expected_dimensions,
                    )
                )
            self._phase = "validating:visual"
            visually_validated: list[dict[str, Any]] = []
            for plan_step_index, item in enumerate(validated):
                visually_validated.append(
                    await self._visual_gate(
                        request=request,
                        plan=self._plan,
                        candidate_build_id=self._candidate_build_id,
                        modeled=item,
                        plan_step_index=plan_step_index,
                        expected_dimensions=expected_dimensions,
                    )
                )
            self._phase = "validating:dfm"
            fully_validated: list[dict[str, Any]] = []
            for plan_step_index, item in enumerate(visually_validated):
                fully_validated.append(
                    await self._dfm_gate(
                        request=request,
                        plan=self._plan,
                        candidate_build_id=self._candidate_build_id,
                        modeled=item,
                        plan_step_index=plan_step_index,
                        expected_dimensions=expected_dimensions,
                    )
                )
            bom_result: dict[str, Any] | None = None
            if native_bom_v1 and self._plan["model_kind"] == "assembly":
                combine_manifest_id = next(
                    item["executed"]["staging_manifest_id"]
                    for item in fully_validated
                    if item["step"]["kind"] == "assembly_combine"
                )
                self._phase = "validating:bom"
                bom_result = await self._activity(
                    "agent_v2.generate_bom",
                    {
                        **request,
                        "candidate_build_id": self._candidate_build_id,
                        "staging_manifest_id": combine_manifest_id,
                        "plan": self._plan,
                    },
                    suffix="native-bom",
                    execution=True,
                )
            manifests = [
                {
                    "step_key": item["step"]["step_key"],
                    "source_id": item["generated"]["source_id"],
                    "source_hash": item["generated"]["source_hash"],
                    "staging_manifest_id": item["executed"][
                        "staging_manifest_id"
                    ],
                    "manifest_hash": item["executed"]["manifest_hash"],
                    "geometry_evidence_id": item["geometry"]["evidence_id"],
                    "visual_evidence_id": (
                        item["visual"]["evidence_id"] if item["visual"] else None
                    ),
                    "dfm_evidence_id": (
                        item["dfm"]["evidence_id"] if item["dfm"] else None
                    ),
                    "bom_evidence_id": (
                        bom_result["evidence_id"]
                        if (
                            bom_result is not None
                            and item["step"]["kind"] == "assembly_combine"
                        )
                        else None
                    ),
                }
                for item in fully_validated
            ]
            self._phase = "sealing"
            sealed = await self._activity(
                "agent_v2.seal_candidate",
                {
                    **request,
                    "candidate_build_id": self._candidate_build_id,
                    "plan": self._plan,
                    "selected_manifests": manifests,
                },
                suffix="seal-candidate",
            )
            self._phase = "reviewable"
            return sealed
        except asyncio.CancelledError:
            return await self._record_cancel(request)
        except Exception as exc:
            if self._provider_streaming_v1 and self._cancel_reason is not None:
                return await self._record_cancel(request)
            self._phase = "failed"
            error_code = "agent_v2_workflow_failed"
            error_message = str(exc)[:4000]
            if (
                isinstance(exc, ActivityError)
                and isinstance(exc.cause, ApplicationError)
            ):
                error_code = (exc.cause.type or error_code)[:200]
                error_message = str(exc.cause)[:4000]
            elif isinstance(exc, ApplicationError):
                error_code = (exc.type or error_code)[:200]
            if self._candidate_build_id is not None:
                await self._activity(
                    "agent_v2.terminate_candidate",
                    {
                        **request,
                        "candidate_build_id": self._candidate_build_id,
                        "target_status": "failed",
                        "error_code": error_code,
                        "error_message": error_message,
                    },
                    suffix="fail-candidate",
                )
            await self._activity(
                "mcad.record_failure",
                {
                    **request,
                    "error_code": error_code,
                    "error_message": error_message,
                },
                suffix="record-failure",
            )
            raise
        finally:
            if request.get("document_queue"):
                await release_document(request)
