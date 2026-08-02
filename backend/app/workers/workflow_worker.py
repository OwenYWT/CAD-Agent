"""Temporal Worker process for durable MCAD workflows."""
from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from app.config import settings
from app.execution.backend import ExecutionBackend
from app.temporal_client import get_temporal_client
from app.workflows.activities import McadWorkflowActivities
from app.workflows.definitions import McadCheckWorkflow, McadDurableWorkflow


def build_workflow_worker(
    client: Client,
    *,
    backend: ExecutionBackend | None = None,
) -> Worker:
    activities = McadWorkflowActivities(backend)
    return Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[McadDurableWorkflow, McadCheckWorkflow],
        activities=activities.registered(),
    )


async def run_worker() -> None:
    client = await get_temporal_client()
    worker = build_workflow_worker(client)
    await worker.run()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
