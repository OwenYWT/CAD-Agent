"""Temporal client boundary and real workflow round-trip probe."""
from __future__ import annotations

import asyncio
import time
import uuid

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker

from app.config import settings


_client: Client | None = None


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
