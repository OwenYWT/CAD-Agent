from __future__ import annotations

import asyncio
from types import MethodType

import pytest


@pytest.mark.asyncio
async def test_activity_observer_returns_business_result_and_records_success(monkeypatch):
    from app.workflows.activities import McadWorkflowActivities

    calls = []
    monkeypatch.setattr(
        "app.workflows.activities.record_activity_result",
        lambda payload, name, result, **kwargs: calls.append((payload, name, result, kwargs)),
    )
    activities = object.__new__(McadWorkflowActivities)
    expected = {"status": "completed", "step_key": "plan"}

    result = await activities._run_observed(
        "agent_v2.plan",
        {"workflow_run_id": "workflow-1"},
        lambda: _resolved(expected),
    )

    assert result is expected
    assert calls[0][1] == "agent_v2.plan"
    assert calls[0][2] is expected


@pytest.mark.asyncio
async def test_activity_observer_reraises_business_error_after_recording(monkeypatch):
    from app.workflows.activities import McadWorkflowActivities

    calls = []
    monkeypatch.setattr(
        "app.workflows.activities.record_activity_failure",
        lambda payload, name, error, **kwargs: calls.append((payload, name, error, kwargs)),
    )
    activities = object.__new__(McadWorkflowActivities)

    with pytest.raises(RuntimeError, match="model failed"):
        await activities._run_observed(
            "agent_v2.execute_freecad",
            {"workflow_run_id": "workflow-2"},
            lambda: _rejected(RuntimeError("model failed")),
        )

    assert calls[0][1] == "agent_v2.execute_freecad"
    assert isinstance(calls[0][2], RuntimeError)


@pytest.mark.asyncio
async def test_activity_observer_does_not_change_business_result_when_recording_fails(monkeypatch):
    from app.workflows.activities import McadWorkflowActivities

    def fail(*args, **kwargs):
        raise OSError("report disk unavailable")

    monkeypatch.setattr("app.workflows.activities.record_activity_result", fail)
    activities = object.__new__(McadWorkflowActivities)
    expected = {"status": "completed"}

    result = await activities._run_observed(
        "agent_v2.plan",
        {"workflow_run_id": "workflow-3"},
        lambda: _resolved(expected),
    )

    assert result is expected


@pytest.mark.asyncio
async def test_activity_observer_propagates_cancellation_without_recording_failure(monkeypatch):
    from app.workflows.activities import McadWorkflowActivities

    calls = []
    monkeypatch.setattr(
        "app.workflows.activities.record_activity_failure",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    activities = object.__new__(McadWorkflowActivities)

    with pytest.raises(asyncio.CancelledError):
        await activities._run_observed(
            "agent_v2.execute_freecad",
            {"workflow_run_id": "workflow-4"},
            _cancelled,
        )

    assert calls == []


@pytest.mark.asyncio
async def test_activity_observer_uses_stable_event_identity_for_retries(monkeypatch):
    from app.workflows.activities import McadWorkflowActivities

    calls = []
    monkeypatch.setattr(
        "app.workflows.activities.record_activity_result",
        lambda payload, name, result, **kwargs: calls.append(kwargs),
    )
    activities = object.__new__(McadWorkflowActivities)
    payload = {
        "workflow_run_id": "workflow-retry",
        "step_key": "geometry",
        "attempt_id": "attempt-1",
    }

    await activities._run_observed("agent_v2.validate_geometry", payload, lambda: _resolved({}))
    await activities._run_observed("agent_v2.validate_geometry", payload, lambda: _resolved({}))

    assert calls[0]["event_id"] == calls[1]["event_id"]


@pytest.mark.asyncio
async def test_activity_observer_records_real_duration_for_success(monkeypatch):
    from app.workflows import activities as activities_module
    from app.workflows.activities import McadWorkflowActivities

    clock = iter((10.0, 11.234))
    calls = []
    monkeypatch.setattr(activities_module.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(
        "app.workflows.activities.record_activity_result",
        lambda payload, name, result, **kwargs: calls.append(kwargs),
    )
    activities = object.__new__(McadWorkflowActivities)

    await activities._run_observed(
        "agent_v2.plan",
        {"workflow_run_id": "workflow-duration-success"},
        lambda: _resolved({"status": "completed"}),
    )

    assert calls[0]["duration_ms"] == 1234
    assert calls[0]["started_at"]
    assert calls[0]["finished_at"]


@pytest.mark.asyncio
async def test_activity_observer_records_real_duration_for_failure(monkeypatch):
    from app.workflows import activities as activities_module
    from app.workflows.activities import McadWorkflowActivities

    clock = iter((20.0, 20.5))
    calls = []
    monkeypatch.setattr(activities_module.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(
        "app.workflows.activities.record_activity_failure",
        lambda payload, name, error, **kwargs: calls.append(kwargs),
    )
    activities = object.__new__(McadWorkflowActivities)

    with pytest.raises(RuntimeError, match="duration failure"):
        await activities._run_observed(
            "agent_v2.execute_freecad",
            {"workflow_run_id": "workflow-duration-failure"},
            lambda: _rejected(RuntimeError("duration failure")),
        )

    assert calls[0]["duration_ms"] == 500
    assert calls[0]["started_at"]
    assert calls[0]["finished_at"]


@pytest.mark.asyncio
async def test_all_agent_v2_activity_wrappers_use_the_observer(monkeypatch):
    from app.workflows import activities as activities_module
    from app.workflows.activities import McadWorkflowActivities

    definitions = {
        "agent_decompose": (activities_module, "decompose", "agent_v2.decompose", {"planner": None}),
        "agent_allocate_candidate": (activities_module.candidates, "agent_allocate_candidate", "agent_v2.allocate_candidate", {}),
        "agent_terminate_candidate": (activities_module.candidates, "agent_terminate_candidate", "agent_v2.terminate_candidate", {}),
        "agent_generate_source": (activities_module.source_generation, "agent_generate_source", "agent_v2.generate_source", {"durable_modeling": None}),
        "agent_repair_source": (activities_module.source_generation, "agent_repair_source", "agent_v2.repair_source", {"durable_repair": None}),
        "agent_generate_operations": (activities_module.native_generation, "agent_generate_operations", "agent_v2.generate_operations", {"freecad_operations": None}),
        "agent_repair_operations": (activities_module.native_generation, "agent_repair_operations", "agent_v2.repair_operations", {"freecad_operations": None}),
        "agent_execute_model": (activities_module.cad_execution, "agent_execute_model", "agent_v2.execute_model", {"backend": None}),
        "agent_execute_freecad": (activities_module.cad_execution, "agent_execute_freecad", "agent_v2.execute_freecad", {"backend": None, "constraint_validation": None}),
        "agent_render_visual": (activities_module.visual_validation, "agent_render_visual", "agent_v2.render_visual", {"backend": None}),
        "agent_judge_visual": (activities_module.visual_validation, "agent_judge_visual", "agent_v2.judge_visual", {"durable_visual": None}),
        "agent_repair_visual": (activities_module.visual_validation, "agent_repair_visual", "agent_v2.repair_visual", {"durable_visual": None}),
        "agent_generate_bom": (activities_module.bom, "agent_generate_bom", "agent_v2.generate_bom", {"backend": None}),
    }
    calls = []

    for method_name, (module, handler_name, activity_name, _kwargs) in definitions.items():
        async def fake_handler(*_args, _activity_name=activity_name, **_kwargs):
            return {"activity": _activity_name}

        monkeypatch.setattr(module, handler_name, fake_handler)

    async def observed(self, activity_name, payload, operation):
        calls.append(activity_name)
        return await operation()

    activities = object.__new__(McadWorkflowActivities)
    activities._run_observed = MethodType(observed, activities)
    for attribute in (
        "durable_modeling",
        "durable_repair",
        "durable_visual",
        "durable_planner",
        "freecad_operations",
        "backend",
    ):
        setattr(activities, attribute, None)

    for method_name, (_module, _handler_name, activity_name, _kwargs) in definitions.items():
        result = await getattr(activities, method_name)({"workflow_run_id": "workflow-5"})
        assert result == {"activity": activity_name}

    assert calls == [item[2] for item in definitions.values()]


async def _resolved(value):
    return value


async def _rejected(error):
    raise error


async def _cancelled():
    raise asyncio.CancelledError
