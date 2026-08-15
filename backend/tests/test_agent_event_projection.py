from uuid import uuid4

from app.services.event_relay import project_task_events


def _event(sequence: int, event_type: str, payload: dict):
    return {
        "id": uuid4(),
        "workflow_run_id": uuid4(),
        "sequence": sequence,
        "event_type": event_type,
        "payload": payload,
        "occurred_at": "2026-08-12T00:00:00Z",
    }


def test_projection_preserves_order_and_maps_repair_and_validation():
    step_id = uuid4()
    attempt_id = uuid4()
    events = project_task_events(
        [
            _event(4, "agent.repair.source_generated", {
                "repair_step_key": "repair-model-main-01",
            }),
            _event(5, "agent.validation_evidence.recorded", {
                "gate": "dfm",
                "mode": "advisory",
                "outcome": "failed",
                "evidence_id": str(uuid4()),
                "evidence_hash": "a" * 64,
            }),
            _event(6, "attempt.state_changed", {
                "attempt_id": str(attempt_id),
                "status": "failed",
                "error_code": "user_code_failed",
                "error_message": "参数尺寸无效",
            }),
        ],
        step_rows=[{
            "id": step_id,
            "step_key": "model-main",
            "kind": "agent_model",
        }],
        attempt_rows=[{
            "id": attempt_id,
            "step_run_id": step_id,
            "attempt_number": 2,
        }],
    )
    assert [item["sequence"] for item in events] == [4, 5, 6]
    assert events[0]["projection"]["stage"] == "repair"
    assert events[1]["projection"]["status"] == "warn"
    assert events[1]["projection"]["evidence_hash"] == "a" * 64
    assert events[2]["projection"]["stage"] == "modeling"
    assert events[2]["projection"]["status"] == "failed"
    assert events[2]["projection"]["message"] == "参数尺寸无效"
    assert events[2]["projection"]["attempt_number"] == 2


def test_required_validation_failure_is_not_downgraded_to_warning():
    event = project_task_events(
        [_event(1, "agent.validation_evidence.recorded", {
            "gate": "geometry",
            "mode": "required",
            "outcome": "indeterminate",
            "evidence_id": str(uuid4()),
            "evidence_hash": "b" * 64,
        })],
        step_rows=[],
        attempt_rows=[],
    )[0]
    assert event["projection"]["status"] == "failed"
    assert event["projection"]["message"] == "几何检查未能判定"
