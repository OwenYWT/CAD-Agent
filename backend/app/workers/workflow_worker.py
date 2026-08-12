"""Temporal Worker process for durable MCAD workflows."""
from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from app.config import settings
from app.agent.durable_planner import DurableAgentPlanner
from app.db import database_readiness
from app.execution.backend import ExecutionBackend
from app.execution.composition import get_execution_backend
from app.object_store import object_store_readiness
from app.temporal_client import get_temporal_client
from app.workflows.activities import McadWorkflowActivities
from app.workflows.modeling import DurableModelingSourceGenerator
from app.workflows.agent_v2 import McadAgentWorkflowV2
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


def build_agent_v2_workflow_worker(
    client: Client,
    *,
    backend: ExecutionBackend | None = None,
    durable_planner: DurableAgentPlanner | None = None,
    durable_modeling: DurableModelingSourceGenerator | None = None,
) -> Worker:
    """Build the version-isolated V2 worker on its dedicated task queue."""
    activities = McadWorkflowActivities(
        backend,
        durable_planner=durable_planner,
        durable_modeling=durable_modeling,
    )
    return Worker(
        client,
        task_queue=settings.temporal_agent_v2_task_queue,
        workflows=[McadAgentWorkflowV2],
        activities=activities.registered_agent_v2(),
    )


async def run_worker() -> None:
    settings.assert_sandbox_config_safe()
    settings.assert_durable_control_plane_config_safe()
    await database_readiness()
    await object_store_readiness()
    backend = get_execution_backend()
    await asyncio.to_thread(backend.runtime_snapshot)
    client = await get_temporal_client()
    v1_worker = build_workflow_worker(client, backend=backend)
    v2_worker = build_agent_v2_workflow_worker(client, backend=backend)
    await asyncio.gather(v1_worker.run(), v2_worker.run())


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
