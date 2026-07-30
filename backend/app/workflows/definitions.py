"""Deterministic Temporal workflow definitions for MCAD operations."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityCancellationType


_CONTROL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=5,
)
_EXECUTION_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=3,
)


@workflow.defn(name="McadDurableWorkflow")
class McadDurableWorkflow:
    """Temporal owns retries, timers, signals, and cancellation propagation."""

    def __init__(self) -> None:
        self._confirmation: dict[str, Any] | None = None
        self._cancel_reason: str | None = None
        self._change_set_id: str | None = None
        self._phase = "pending"

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

    async def _activity(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        suffix: str,
        execution: bool = False,
    ) -> dict[str, Any]:
        workflow_id = str(payload["workflow_run_id"])
        timeout = int(
            payload.get("execution", {}).get("timeout_seconds", 60)
            if execution
            else 60
        )
        activity_task = asyncio.create_task(
            workflow.execute_activity(
                name,
                payload,
                activity_id=f"{workflow_id}:{suffix}",
                start_to_close_timeout=timedelta(
                    seconds=max(120, timeout + 90)
                ),
                heartbeat_timeout=(
                    timedelta(seconds=15) if execution else None
                ),
                retry_policy=_EXECUTION_RETRY if execution else _CONTROL_RETRY,
                cancellation_type=(
                    ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
                ),
                result_type=dict,
            )
        )
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
        result = await workflow.execute_activity(
            "mcad.record_cancel",
            {
                **request,
                "reason": self._cancel_reason or "cancelled",
                "change_set_id": self._change_set_id,
            },
            activity_id=f"{request['workflow_run_id']}:record-cancel",
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_CONTROL_RETRY,
            result_type=dict,
        )
        self._phase = "cancelled"
        return result

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            self._phase = "planning"
            plan = await self._activity(
                "mcad.plan",
                request,
                suffix="plan",
            )
            self._change_set_id = str(plan["change_set_id"])
            execution_payload = {
                **request,
                "revision_id": plan["candidate_revision_id"],
                "change_set_id": plan["change_set_id"],
                "execution": request["primary"],
                "step_index": 1,
            }
            self._phase = request["primary"]["step_key"]
            primary = await self._activity(
                "mcad.execute",
                execution_payload,
                suffix=f"execute:{request['primary']['step_key']}",
                execution=True,
            )
            self._phase = "validate"
            validation = await self._activity(
                "mcad.validate",
                {
                    **request,
                    "revision_id": plan["candidate_revision_id"],
                    "change_set_id": plan["change_set_id"],
                    "execution_result": primary,
                    "step_key": "validate-primary",
                    "step_index": 2,
                },
                suffix="validate:primary",
            )

            if request.get("require_confirmation", True):
                self._phase = "waiting_confirmation"
                await self._activity(
                    "mcad.wait_confirmation",
                    {
                        **request,
                        "change_set_id": plan["change_set_id"],
                    },
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
                        timeout_summary="MCAD confirmation deadline",
                    )
                except asyncio.TimeoutError:
                    self._phase = "timed_out"
                    return await workflow.execute_activity(
                        "mcad.record_timeout",
                        {
                            **request,
                            "change_set_id": plan["change_set_id"],
                        },
                        activity_id=f"{request['workflow_run_id']}:timeout",
                        start_to_close_timeout=timedelta(seconds=60),
                        retry_policy=_CONTROL_RETRY,
                        result_type=dict,
                    )
                if self._cancel_reason is not None:
                    return await self._record_cancel(request)
                if not bool(self._confirmation and self._confirmation["accepted"]):
                    self._cancel_reason = (
                        str(self._confirmation.get("note") or "用户拒绝变更")
                        if self._confirmation
                        else "用户拒绝变更"
                    )
                    return await self._record_cancel(request)
                await self._activity(
                    "mcad.resume_after_confirmation",
                    {
                        **request,
                        "change_set_id": plan["change_set_id"],
                        "note": str(self._confirmation.get("note") or ""),
                    },
                    suffix="resume-confirmation",
                )

            followup_result: dict[str, Any] | None = None
            if request.get("followup"):
                followup = request["followup"]
                self._phase = followup["step_key"]
                followup_result = await self._activity(
                    "mcad.execute",
                    {
                        **request,
                        "revision_id": plan["candidate_revision_id"],
                        "change_set_id": plan["change_set_id"],
                        "execution": followup,
                        "step_index": 3,
                    },
                    suffix=f"execute:{followup['step_key']}",
                    execution=True,
                )
                self._phase = "validate_followup"
                validation = await self._activity(
                    "mcad.validate",
                    {
                        **request,
                        "revision_id": plan["candidate_revision_id"],
                        "change_set_id": plan["change_set_id"],
                        "execution_result": followup_result,
                        "step_key": "validate-followup",
                        "step_index": 4,
                    },
                    suffix="validate:followup",
                )

            self._phase = "finalizing"
            finalized = await self._activity(
                "mcad.finalize",
                {
                    **request,
                    "change_set_id": plan["change_set_id"],
                    "revision_id": plan["candidate_revision_id"],
                    "validation": validation,
                },
                suffix="finalize",
            )
            self._phase = finalized["status"]
            return {
                **finalized,
                "workflow_run_id": request["workflow_run_id"],
                "revision_id": plan["candidate_revision_id"],
                "change_set_id": plan["change_set_id"],
                "primary": primary,
                "followup": followup_result,
                "validation": validation,
            }
        except asyncio.CancelledError:
            return await self._record_cancel(request)
        except Exception as exc:
            self._phase = "failed"
            await workflow.execute_activity(
                "mcad.record_failure",
                {
                    **request,
                    "error_message": str(exc)[:4000],
                },
                activity_id=f"{request['workflow_run_id']}:record-failure",
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=_CONTROL_RETRY,
                result_type=dict,
            )
            raise
