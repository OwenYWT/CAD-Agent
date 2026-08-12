"""V1/V2 Temporal Worker registration must remain isolated."""
from __future__ import annotations

from app.config import settings
from app.workers.workflow_worker import (
    build_agent_v2_workflow_worker,
    build_workflow_worker,
)


def test_v1_and_v2_workers_use_distinct_queues_and_workflow_types(monkeypatch):
    captured: list[dict] = []

    class CapturingWorker:
        def __init__(self, client, **kwargs):
            captured.append({"client": client, **kwargs})

    monkeypatch.setattr("app.workers.workflow_worker.Worker", CapturingWorker)
    client = object()
    backend = object()

    build_workflow_worker(client, backend=backend)
    build_agent_v2_workflow_worker(client, backend=backend)

    assert captured[0]["task_queue"] == settings.temporal_task_queue
    assert captured[1]["task_queue"] == settings.temporal_agent_v2_task_queue
    assert captured[0]["task_queue"] != captured[1]["task_queue"]
    assert [item.__name__ for item in captured[0]["workflows"]] == [
        "McadDurableWorkflow",
        "McadCheckWorkflow",
    ]
    assert [item.__name__ for item in captured[1]["workflows"]] == [
        "McadAgentWorkflowV2",
    ]
    assert {item.__temporal_activity_definition.name for item in captured[1]["activities"]} == {
        "agent_v2.requirements",
        "agent_v2.decompose",
        "agent_v2.plan",
        "agent_v2.allocate_candidate",
        "agent_v2.terminate_candidate",
        "agent_v2.generate_source",
        "agent_v2.repair_source",
        "agent_v2.execute_model",
        "mcad.wait_confirmation",
        "mcad.resume_after_confirmation",
        "mcad.record_cancel",
        "mcad.record_timeout",
        "mcad.record_failure",
    }
    assert all(
        item.__temporal_activity_definition.name not in {
            "agent_v2.requirements",
            "agent_v2.decompose",
            "agent_v2.plan",
        }
        for item in captured[0]["activities"]
    )
