"""Version-isolated Temporal definition for the fused durable Agent flow."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.workflow import ActivityCancellationType


_CONTROL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=3,
)


@workflow.defn(name="McadAgentWorkflowV2")
class McadAgentWorkflowV2:
    """Persist planning first and block execution at an explicit gate."""

    def __init__(self) -> None:
        self._confirmation: dict[str, Any] | None = None
        self._cancel_reason: str | None = None
        self._phase = "pending"
        self._plan: dict[str, Any] | None = None
        self._candidate_build_id: str | None = None

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
        activity_task = asyncio.create_task(
            workflow.execute_activity(
                name,
                payload,
                activity_id=f"{payload['workflow_run_id']}:{suffix}",
                start_to_close_timeout=(
                    timedelta(seconds=timeout_seconds + 120)
                    if execution
                    else timedelta(minutes=5)
                ),
                heartbeat_timeout=(timedelta(seconds=15) if execution else None),
                retry_policy=_CONTROL_RETRY,
                cancellation_type=(
                    ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
                ),
                result_type=dict,
            )
        )
        if not execution:
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
        executed = await self._activity(
            "agent_v2.execute_model",
            {
                **request,
                "candidate_build_id": candidate_build_id,
                "plan": plan,
                "step": step,
                "step_index": step_index,
                "source_id": generated["source_id"],
                "source_hash": generated["source_hash"],
                "source_code": generated["source_code"],
                "mode": generated["mode"],
                "timeout_seconds": 120,
            },
            suffix=f"execute-model-{step['step_key']}",
            execution=True,
        )
        return {"step": step, "generated": generated, "executed": executed}

    async def _model_step_outcome(self, **kwargs: Any) -> dict[str, Any]:
        try:
            return {"result": await self._model_step(**kwargs), "error": None}
        except Exception as exc:
            return {"result": None, "error": exc}

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
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
            requirements_payload = (
                requirements["requirements"]
                if request["operation"] == "generate"
                else {
                    "existing_code": request.get("existing_code") or "",
                    "modification_plan": requirements["requirements"],
                }
            )
            modeling_offset = 3 if request["operation"] == "generate" else 2
            modeled: list[dict[str, Any]] = []
            if self._plan["model_kind"] == "assembly":
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
            self._phase = "validation_not_enabled"
            raise ApplicationError(
                "Durable Agent modeling completed, but required validation is "
                f"not enabled yet for candidate {candidate['candidate_build_id']}.",
                type="agent_v2_validation_not_enabled",
                non_retryable=True,
            )
        except asyncio.CancelledError:
            return await self._record_cancel(request)
        except Exception as exc:
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
