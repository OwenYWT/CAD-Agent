"""Temporal Worker process for durable MCAD workflows."""
from __future__ import annotations

from functools import partial
from app.workflows.handlers import planning, decomposition, source_generation, native_generation, visual_validation
import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from app.workers.usage_interceptor import UsageWorkerInterceptor
from app.config import settings
from app.agent.durable_planner import DurableAgentPlanner
from app.agent.durable_repair import DurableRepairSourceGenerator
from app.db import database_readiness
from app.execution.backend import ExecutionBackend
from app.execution.composition import get_execution_backend
from app.freecad.operation_generator import FreeCADOperationGenerator
from app.object_store import object_store_readiness
from app.temporal_client import get_temporal_client
from app.workflows.activities import McadWorkflowActivities
from app.workflows.modeling import DurableModelingSourceGenerator
from app.workflows.agent_v2 import McadAgentWorkflowV2
from app.validation.durable_visual import DurableVisualValidator
from app.workflows.definitions import McadCheckWorkflow, McadDurableWorkflow
from app.services.workflow_dispatch import run_dispatcher, dispatcher_readiness
from app.services.model_jobs import submit_activity, read_activity, cancel_activity
from app.workers.model_job_worker import ModelJobWorker
from app.workflows.model_job import ModelJobWorkflow
from app.workflows.model_job_policy import MODEL_OPERATIONS


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
        interceptors=[UsageWorkerInterceptor()],
    )


def build_agent_v2_workflow_worker(
    client: Client,
    *,
    backend: ExecutionBackend | None = None,
    durable_planner: DurableAgentPlanner | None = None,
    durable_modeling: DurableModelingSourceGenerator | None = None,
    durable_repair: DurableRepairSourceGenerator | None = None,
    durable_visual: DurableVisualValidator | None = None,
    freecad_operations: FreeCADOperationGenerator | None = None,
) -> Worker:
    """Build the version-isolated V2 worker on its dedicated task queue."""
    activities = McadWorkflowActivities(
        backend,
        durable_planner=durable_planner,
        durable_modeling=durable_modeling,
        durable_repair=durable_repair,
        durable_visual=durable_visual,
        freecad_operations=freecad_operations,
    )
    operations = {
        'agent_v2.requirements': partial(planning.agent_requirements, durable_planner=activities.durable_planner),
        'agent_v2.decompose': partial(decomposition.decompose, planner=activities.durable_planner),
        'agent_v2.generate_source': partial(source_generation.agent_generate_source, durable_modeling=activities.durable_modeling),
        'agent_v2.repair_source': partial(source_generation.agent_repair_source, durable_repair=activities.durable_repair),
        'agent_v2.generate_operations': partial(native_generation.agent_generate_operations, freecad_operations=activities.freecad_operations),
        'agent_v2.repair_operations': partial(native_generation.agent_repair_operations, freecad_operations=activities.freecad_operations),
        'agent_v2.judge_visual': partial(visual_validation.agent_judge_visual, durable_visual=activities.durable_visual),
        'agent_v2.repair_visual': partial(visual_validation.agent_repair_visual, durable_visual=activities.durable_visual),
    }
    if operations.keys() != MODEL_OPERATIONS:
        raise RuntimeError('Model operation registration differs from the durable contract')
    return ModelJobWorker(
        client,
        model_operations=operations,
        task_queue=settings.temporal_agent_v2_task_queue,
        workflows=[McadAgentWorkflowV2,ModelJobWorkflow],
        activities=[*activities.registered_agent_v2(),submit_activity,read_activity,cancel_activity],
        interceptors=[UsageWorkerInterceptor()],
    )


async def run_worker() -> None:
    settings.assert_sandbox_config_safe()
    settings.assert_durable_control_plane_config_safe()
    await database_readiness()
    await dispatcher_readiness()
    await object_store_readiness()
    backend = get_execution_backend()
    await asyncio.to_thread(backend.runtime_snapshot)
    client = await get_temporal_client()
    v1_worker = build_workflow_worker(client, backend=backend)
    v2_worker = build_agent_v2_workflow_worker(client, backend=backend)
    await asyncio.gather(v1_worker.run(), v2_worker.run(), run_dispatcher(
        client, task_queues=(settings.temporal_task_queue, settings.temporal_agent_v2_task_queue),
    ))


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
