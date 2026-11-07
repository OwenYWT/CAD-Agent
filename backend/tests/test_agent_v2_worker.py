"""V1/V2 Temporal Worker registration must remain isolated."""
from __future__ import annotations

import asyncio

import pytest

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
    monkeypatch.setattr("app.workers.workflow_worker.ModelJobWorker", CapturingWorker)
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
        "ModelJobWorkflow",
    ]
    assert {item.__temporal_activity_definition.name for item in captured[1]["activities"]} == {
        "model_jobs.submit",
        "model_jobs.read",
        "model_jobs.cancel",
        "document.acquire",
        "document.release",
        "agent_v2.requirements",
        "agent_v2.decompose",
        "agent_v2.plan",
        "agent_v2.allocate_candidate",
        "agent_v2.terminate_candidate",
        "agent_v2.generate_source",
        "agent_v2.generate_operations",
        "agent_v2.repair_source",
        "agent_v2.repair_operations",
        "agent_v2.execute_model",
        "agent_v2.execute_freecad",
        "agent_v2.validate_geometry",
        "agent_v2.render_visual",
        "agent_v2.judge_visual",
        "agent_v2.repair_visual",
        "agent_v2.validate_dfm",
        "agent_v2.generate_bom",
        "agent_v2.seal_candidate",
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


@pytest.mark.asyncio
async def test_v2_model_operation_uses_live_observer_without_changing_result(monkeypatch):
    captured: list[dict] = []
    observed = []
    expected = {"status": "completed", "step_key": "requirements"}

    class CapturingWorker:
        def __init__(self, client, **kwargs):
            captured.append({"client": client, **kwargs})

    async def requirements(_payload, *, durable_planner):
        assert durable_planner is not None
        return expected

    monkeypatch.setattr("app.workers.workflow_worker.ModelJobWorker", CapturingWorker)
    monkeypatch.setattr("app.workers.workflow_worker.planning.agent_requirements", requirements)
    monkeypatch.setattr(
        "app.evaluation.integration.record_activity_result",
        lambda payload, name, result, **kwargs: observed.append(
            (payload, name, result, kwargs)
        ),
    )

    build_agent_v2_workflow_worker(object(), backend=object())
    operation = captured[0]["model_operations"]["agent_v2.requirements"]
    payload = {"workflow_run_id": "workflow-model-operation"}

    result = await operation(payload)

    assert result is expected
    assert observed[0][0] is payload
    assert observed[0][1] == "agent_v2.requirements"
    assert observed[0][2] is expected


@pytest.mark.asyncio
async def test_v2_model_operation_records_real_duration_for_success(monkeypatch):
    from app.evaluation import integration as integration_module
    from app.evaluation.integration import observe_model_operation

    clock = iter((10.0, 11.234))
    observed = []
    monkeypatch.setattr(
        integration_module,
        "time",
        type("Clock", (), {"perf_counter": staticmethod(lambda: next(clock))}),
        raising=False,
    )
    monkeypatch.setattr(
        "app.evaluation.integration.record_activity_result",
        lambda payload, name, result, **kwargs: observed.append(kwargs),
    )

    async def operation(_payload):
        return {"status": "completed"}

    result = await observe_model_operation(
        "agent_v2.generate_source",
        operation,
    )({"workflow_run_id": "workflow-duration-success"})

    assert result == {"status": "completed"}
    assert observed[0]["duration_ms"] == 1234
    assert observed[0]["started_at"]
    assert observed[0]["finished_at"]


@pytest.mark.asyncio
async def test_v2_model_operation_records_real_duration_for_failure(monkeypatch):
    from app.evaluation import integration as integration_module
    from app.evaluation.integration import observe_model_operation

    clock = iter((20.0, 20.5))
    failures = []
    monkeypatch.setattr(
        integration_module,
        "time",
        type("Clock", (), {"perf_counter": staticmethod(lambda: next(clock))}),
        raising=False,
    )
    monkeypatch.setattr(
        "app.evaluation.integration.record_activity_failure",
        lambda payload, name, error, **kwargs: failures.append(kwargs),
    )

    async def operation(_payload):
        raise RuntimeError("duration failure")

    with pytest.raises(RuntimeError, match="duration failure"):
        await observe_model_operation(
            "agent_v2.execute_freecad",
            operation,
        )({"workflow_run_id": "workflow-duration-failure"})

    assert failures[0]["duration_ms"] == 500
    assert failures[0]["started_at"]
    assert failures[0]["finished_at"]


@pytest.mark.asyncio
async def test_v2_model_operation_observer_preserves_error_and_cancellation(monkeypatch):
    from app.evaluation.integration import observe_model_operation

    failures = []
    monkeypatch.setattr(
        "app.evaluation.integration.record_activity_failure",
        lambda payload, name, error, **kwargs: failures.append(
            (payload, name, error, kwargs)
        ),
    )

    async def fail(_payload):
        raise RuntimeError("provider failed")

    async def cancel(_payload):
        raise asyncio.CancelledError

    with pytest.raises(RuntimeError, match="provider failed"):
        await observe_model_operation("agent_v2.generate_source", fail)(
            {"workflow_run_id": "workflow-failed-operation"}
        )
    with pytest.raises(asyncio.CancelledError):
        await observe_model_operation("agent_v2.generate_source", cancel)(
            {"workflow_run_id": "workflow-cancelled-operation"}
        )

    assert len(failures) == 1
    assert failures[0][1] == "agent_v2.generate_source"
    assert isinstance(failures[0][2], RuntimeError)


@pytest.mark.asyncio
async def test_v2_model_operation_ignores_observer_failure(monkeypatch):
    from app.evaluation.integration import observe_model_operation

    expected = {"status": "completed"}

    async def operation(_payload):
        return expected

    def fail_observer(*_args, **_kwargs):
        raise OSError("report storage unavailable")

    monkeypatch.setattr(
        "app.evaluation.integration.record_activity_result",
        fail_observer,
    )

    result = await observe_model_operation(
        "agent_v2.generate_source",
        operation,
    )({"workflow_run_id": "workflow-observer-failure"})

    assert result is expected
