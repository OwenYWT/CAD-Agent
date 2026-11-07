from __future__ import annotations

import json


def test_file_store_appends_events_across_process_local_instances(tmp_path):
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.store import FileEvaluationStore

    first = EvaluationSession.create(
        mode=EvaluationMode.OBSERVE,
        request_id="request-1",
        task_id="task-1",
        workflow_id="workflow-1",
    )
    store_a = FileEvaluationStore(tmp_path)
    store_b = FileEvaluationStore(tmp_path)
    store_a.create(first)
    store_b.append_event(
        first.evaluation_run_id,
        event_type="geometry_validation_finished",
        status="pass",
        payload={"validator": "geometry-v1"},
    )

    report = json.loads(store_a.path_for(first.evaluation_run_id).read_text(encoding="utf-8"))
    assert [item["sequence"] for item in report["timeline"]] == [1]
    assert report["timeline"][0]["event_type"] == "geometry_validation_finished"


def test_file_store_deduplicates_temporal_activity_retry(tmp_path):
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.store import FileEvaluationStore

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    store = FileEvaluationStore(tmp_path)
    store.create(session)

    event_id = "workflow-1:agent_v2.validate_geometry"
    for _ in range(2):
        store.append_event(
            session.evaluation_run_id,
            event_id=event_id,
            event_type="geometry_validation_finished",
            status="pass",
            payload={"outcome": "passed"},
        )

    report = json.loads(store.path_for(session.evaluation_run_id).read_text(encoding="utf-8"))
    assert len(report["timeline"]) == 1


def test_file_store_identity_update_and_terminal_status_are_atomic(tmp_path):
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.store import FileEvaluationStore

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    store = FileEvaluationStore(tmp_path)
    store.create(session)
    store.update_identity(session.evaluation_run_id, candidate_revision_id="candidate-1")
    store.finish(session.evaluation_run_id, "finished")

    report = json.loads(store.path_for(session.evaluation_run_id).read_text(encoding="utf-8"))
    assert report["candidate_revision_id"] == "candidate-1"
    assert report["status"] == "finished"
    assert report["finished_at"]


def test_file_store_keeps_markdown_report_in_sync_with_live_events(tmp_path):
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.store import FileEvaluationStore
    from app.evaluation.writer import human_report_path

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE, workflow_id="workflow-1")
    store = FileEvaluationStore(tmp_path)
    store.create(session)
    store.append_event(
        session.evaluation_run_id,
        event_type="geometry_validation_finished",
        status="pass",
        payload={"outcome": "passed", "status": "completed"},
    )
    store.finish(session.evaluation_run_id, "finished")

    markdown = human_report_path(tmp_path, session.evaluation_run_id).read_text(
        encoding="utf-8"
    )
    assert "\u51e0\u4f55\u6821\u9a8c: \u901a\u8fc7" in markdown
    assert "\u6700\u7ec8\u72b6\u6001: \u5df2\u5b8c\u6210" in markdown
