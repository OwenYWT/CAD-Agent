"""Version-isolated Temporal definition for the fused durable Agent flow."""
from __future__ import annotations

from temporalio import workflow
from temporalio.exceptions import ApplicationError


@workflow.defn(name="McadAgentWorkflowV2")
class McadAgentWorkflowV2:
    """Fail closed until V2 planning activities are enabled and routed."""

    @workflow.run
    async def run(self, request: dict) -> dict:
        raise ApplicationError(
            "Durable Agent V2 submission is not enabled.",
            type="agent_v2_not_enabled",
            non_retryable=True,
        )
