import pytest
from pydantic import ValidationError

from app.models.schemas import StepUpdate


def test_step_update_remains_backward_compatible():
    step = StepUpdate(step="planning", message="Planning model")
    payload = step.model_dump()

    assert payload["step"] == "planning"
    assert payload["message"] == "Planning model"
    assert payload["status"] == "running"
    assert payload["stage_id"] is None
    assert payload["attempt"] is None
    assert payload["started_at"] is None
    assert payload["duration_ms"] is None
    assert payload["detail"] is None


def test_step_update_accepts_structured_timeline_fields():
    step = StepUpdate(
        step="repairing_code",
        message="Repairing code after execution error",
        status="warn",
        stage_id="repairing_code:2",
        attempt=2,
        started_at="2026-07-19T09:01:02.003Z",
        duration_ms=2410,
        detail={"error_type": "NameError", "source": "execution"},
    )

    assert step.status == "warn"
    assert step.stage_id == "repairing_code:2"
    assert step.attempt == 2
    assert step.duration_ms == 2410
    assert step.detail == {"error_type": "NameError", "source": "execution"}


def test_step_update_rejects_unknown_status():
    with pytest.raises(ValidationError):
        StepUpdate(step="planning", message="Planning model", status="thinking")

from app.agent.run_steps import RunStageTimer, make_step


def test_make_step_creates_stable_stage_id_and_detail():
    step = make_step(
        "executing",
        "Running sandbox",
        status="running",
        attempt=1,
        detail={"source": "sandbox"},
    )

    assert step.step == "executing"
    assert step.status == "running"
    assert step.stage_id == "executing:1"
    assert step.started_at is not None
    assert step.detail == {"source": "sandbox"}


def test_run_stage_timer_completes_with_duration():
    timer = RunStageTimer("validating_geometry", "Checking model", attempt=1)
    started = timer.start()
    completed = timer.complete("Geometry validation complete", status="success")

    assert started.status == "running"
    assert completed.status == "success"
    assert completed.stage_id == "validating_geometry:1"
    assert completed.duration_ms is not None
    assert completed.duration_ms >= 0

from app.agent.run_steps import ensure_timeline_fields


def test_ensure_timeline_fields_derives_status_for_legacy_steps():
    step = ensure_timeline_fields(StepUpdate(step="fixing_error", message="Repairing"))

    assert step.status == "warn"
    assert step.stage_id == "fixing_error"
    assert step.started_at is not None
    assert step.message == "Repairing"
