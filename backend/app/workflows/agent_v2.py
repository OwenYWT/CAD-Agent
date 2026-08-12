"""Version-isolated Temporal definition for the fused durable Agent flow."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError


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
    ) -> dict[str, Any]:
        return await workflow.execute_activity(
            name,
            payload,
            activity_id=f"{payload['workflow_run_id']}:{suffix}",
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_CONTROL_RETRY,
            result_type=dict,
        )

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
            self._phase = "modeling_not_enabled"
            raise ApplicationError(
                "Durable Agent modeling is not enabled yet for candidate "
                f"{candidate['candidate_build_id']}.",
                type="agent_v2_modeling_not_enabled",
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
