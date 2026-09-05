from app.api.tasks import _task_snapshot_marker


def test_waiting_confirmation_changes_the_stream_snapshot_marker() -> None:
    pending = {
        "status": "pending",
        "confirmation": None,
        "change_set": None,
    }
    waiting = {
        "status": "waiting_confirmation",
        "confirmation": {
            "workflow_run_id": "workflow-1",
            "plan_hash": "a" * 64,
        },
        "change_set": None,
    }

    assert _task_snapshot_marker(pending) != _task_snapshot_marker(waiting)


def test_identical_confirmation_snapshot_is_not_republished() -> None:
    snapshot = {
        "status": "waiting_confirmation",
        "confirmation": {
            "workflow_run_id": "workflow-1",
            "plan_hash": "a" * 64,
        },
        "change_set": {
            "id": "change-1",
            "status": "pending_review",
        },
    }

    assert _task_snapshot_marker(snapshot) == _task_snapshot_marker(dict(snapshot))
