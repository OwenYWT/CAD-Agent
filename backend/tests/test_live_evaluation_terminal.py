from __future__ import annotations

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "handler_name", "activity_name", "result"),
    [
        ("finalize", "finalize", "mcad.finalize", {"status": "succeeded"}),
        ("record_cancel", "record_cancel", "mcad.record_cancel", {"status": "cancelled"}),
        ("record_timeout", "record_timeout", "mcad.record_timeout", {"status": "timed_out"}),
        ("record_failure", "record_failure", "mcad.record_failure", {"status": "failed"}),
    ],
)
async def test_terminal_activity_finishes_live_report_after_business_success(
    monkeypatch, method_name, handler_name, activity_name, result
):
    from app.workflows import activities as activities_module
    from app.workflows.activities import McadWorkflowActivities

    calls = []

    async def handler(_payload):
        return result

    async def observed(self, name, payload, operation):
        calls.append(("observed", name))
        return await operation()

    def terminal(payload, terminal_result):
        calls.append(("terminal", payload, terminal_result))

    handler_module = activities_module.review if method_name == "finalize" else activities_module.lifecycle
    monkeypatch.setattr(handler_module, handler_name, handler)
    monkeypatch.setattr(activities_module, "record_workflow_terminal", terminal)

    activities = object.__new__(McadWorkflowActivities)
    activities._run_observed = observed.__get__(activities, McadWorkflowActivities)
    payload = {"workflow_run_id": "workflow-terminal-1"}

    actual = await getattr(activities, method_name)(payload)

    assert actual is result
    assert calls == [
        ("observed", activity_name),
        ("terminal", payload, result),
    ]
