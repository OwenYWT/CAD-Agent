"""Temporal client boundary and real workflow round-trip probe."""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio import workflow
from temporalio.client import Client
from temporalio.service import RPCError
from temporalio.worker import Worker

from app.config import settings


_client: Client | None = None


class TemporalWorkerUnavailable(RuntimeError):
    """Submission cannot currently reach a recent worker on its exact queue."""


async def get_temporal_client() -> Client:
    global _client
    if _client is None:
        if not settings.temporal_target:
            raise RuntimeError("TEMPORAL_TARGET is not configured")
        _client = await Client.connect(
            settings.temporal_target,
            namespace=settings.temporal_namespace,
        )
    return _client


def reset_temporal_client() -> None:
    """Drop the cached client after configuration changes in tests or shutdown."""
    global _client
    _client = None


async def temporal_readiness() -> dict:
    started = time.perf_counter()

    async def _probe() -> None:
        client = await get_temporal_client()
        await client.service_client.check_health()

    await asyncio.wait_for(
        _probe(),
        timeout=settings.dependency_readiness_timeout_s,
    )
    return {
        "status": "ready",
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


async def _worker_readiness(task_queue: str) -> dict:
    """Require recent workflow and activity pollers on one exact queue."""
    started = time.perf_counter()

    async def _probe() -> dict[str, int | float]:
        client = await get_temporal_client()
        counts: dict[str, int | float] = {}
        now = datetime.now(timezone.utc)
        for name, queue_type in (
            ("workflow", TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW),
            ("activity", TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY),
        ):
            result = await client.workflow_service.describe_task_queue(
                DescribeTaskQueueRequest(
                    namespace=settings.temporal_namespace,
                    task_queue=TaskQueue(
                        name=task_queue,
                    ),
                    task_queue_type=queue_type,
                    report_pollers=True,
                )
            )
            if not result.pollers:
                raise TemporalWorkerUnavailable(
                    f"Temporal {name} worker has no active poller"
                )
            ages = [
                max(
                    0.0,
                    (
                        now
                        - poller.last_access_time.ToDatetime(
                            tzinfo=timezone.utc
                        )
                    ).total_seconds(),
                )
                for poller in result.pollers
            ]
            # Temporal workflow-task long polls can legitimately remain open for
            # about one minute. Ninety seconds catches a lost worker without
            # marking a healthy idle queue unavailable.
            if min(ages) > 90:
                raise TemporalWorkerUnavailable(
                    f"Temporal {name} worker poller is stale"
                )
            counts[f"{name}_pollers"] = len(result.pollers)
            counts[f"{name}_newest_age_ms"] = round(min(ages) * 1000)
        return counts

    try:
        counts = await asyncio.wait_for(
            _probe(),
            timeout=settings.dependency_readiness_timeout_s,
        )
    except (TimeoutError, RPCError, OSError) as exc:
        raise TemporalWorkerUnavailable("Temporal worker readiness probe is unavailable") from exc
    return {
        "status": "ready",
        "latency_ms": round((time.perf_counter() - started) * 1000),
        **counts,
    }


async def temporal_worker_readiness() -> dict:
    """Require recent V1 workflow and activity pollers."""
    return await _worker_readiness(settings.temporal_task_queue)


async def temporal_agent_v2_worker_readiness() -> dict:
    """Require a V2 poller before any submission routing can be enabled."""
    return await _worker_readiness(settings.temporal_agent_v2_task_queue)


@workflow.defn(sandboxed=False)
class InfrastructureRoundTripWorkflow:
    @workflow.run
    async def run(self, marker: str) -> str:
        return marker


async def temporal_round_trip() -> dict:
    """Execute one actual workflow through the configured Temporal service."""
    client = await get_temporal_client()
    marker = f"cad-agent-temporal-probe-{uuid.uuid4()}"
    task_queue = f"{settings.temporal_task_queue}-readiness"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[InfrastructureRoundTripWorkflow],
    ):
        result = await client.execute_workflow(
            InfrastructureRoundTripWorkflow.run,
            marker,
            id=marker,
            task_queue=task_queue,
        )
    if result != marker:
        raise RuntimeError("Temporal workflow round-trip returned unexpected data")
    return {"status": "success", "workflow_id": marker, "result": result}
