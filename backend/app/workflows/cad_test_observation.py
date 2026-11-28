"""Durable, independently scheduled scoring of a sealed candidate."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class CadTestObservationWorkflow:
    @workflow.run
    async def run(self, observation: dict) -> None:
        await workflow.execute_activity(
            "evaluation.observe_sealed_candidate",
            observation,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
