from __future__ import annotations

import pytest


def test_session_emits_monotonic_events_with_stable_identity():
    from app.evaluation.contracts import EvaluationMode, EvaluationStatus
    from app.evaluation.session import EvaluationSession

    session = EvaluationSession.create(
        mode=EvaluationMode.OBSERVE,
        source_kind="user_task",
        request_id="request-1",
        task_id="task-1",
        workflow_id="workflow-1",
    )

    session.emit("plan_created", status="pass", payload={"plan_id": "plan-1"})
    session.emit("operation_finished", status="pass", payload={"operation_id": "op-1"})

    events = session.to_report()["timeline"]
    assert [event["sequence"] for event in events] == [1, 2]
    assert all(event["request_id"] == "request-1" for event in events)
    assert all(event["task_id"] == "task-1" for event in events)
    assert session.status is EvaluationStatus.RUNNING


def test_session_allows_later_business_identity_without_fabricating_early_ids():
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    session.emit("session_started", payload={"source": "user_task"})
    session.set_identity(candidate_revision_id="candidate-1", change_set_id="change-1")
    session.emit("candidate_sealed", status="pass", payload={"candidate": "candidate-1"})

    report = session.to_report()
    assert report["timeline"][0]["candidate_revision_id"] is None
    assert report["timeline"][1]["candidate_revision_id"] == "candidate-1"
    assert report["timeline"][1]["change_set_id"] == "change-1"


def test_session_preserves_revision_identity_when_supplied():
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession

    session = EvaluationSession.create(
        mode=EvaluationMode.OBSERVE,
        evaluation_run_id="live-revision-1",
        workflow_id="workflow-1",
        base_revision_id="base-1",
        source_revision_id="source-1",
    )

    report = session.to_report()

    assert report["base_revision_id"] == "base-1"
    assert report["source_revision_id"] == "source-1"


@pytest.mark.parametrize(
    "terminal_status",
    ["finished", "failed", "blocked", "cancelled", "error"],
)
def test_session_serializes_terminal_status_and_rejects_later_events(terminal_status):
    from app.evaluation.contracts import EvaluationMode, EvaluationStatus
    from app.evaluation.session import EvaluationSession

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    session.finish(terminal_status)

    assert session.status is EvaluationStatus(terminal_status)
    assert session.to_report()["status"] == terminal_status
    with pytest.raises(RuntimeError, match="terminal"):
        session.emit("after_terminal")


def test_session_rejects_secret_shaped_payload_fields():
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)

    with pytest.raises(ValueError, match="sensitive"):
        session.emit("session_started", payload={"api_key": "secret"})


def test_report_has_versioned_schema_and_developer_source_metadata():
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession

    session = EvaluationSession.create(
        mode=EvaluationMode.OBSERVE,
        source_kind="benchmark",
        source_id="P01",
    )
    report = session.to_report()

    assert report["schema_version"] == "cad-live-evaluation-report.v1"
    assert report["mode"] == "observe"
    assert report["source"] == {"kind": "benchmark", "id": "P01"}
    assert report["evaluation_run_id"]
    assert report["timeline"] == []


def test_evidence_contract_preserves_real_reference_and_nullable_identity():
    from app.evaluation.contracts import EvaluationEvidence

    evidence = EvaluationEvidence(
        evidence_id="evidence-1",
        source_kind="geometry_validator",
        source_ref="validation-1",
        authority="independent_geometry",
        artifact_sha256="a" * 64,
        observed={"bbox_mm": [10.0, 10.0, 10.0]},
    )

    assert evidence.to_dict() == {
        "evidence_id": "evidence-1",
        "source_kind": "geometry_validator",
        "source_ref": "validation-1",
        "authority": "independent_geometry",
        "artifact_sha256": "a" * 64,
        "observed": {"bbox_mm": [10.0, 10.0, 10.0]},
        "details": {},
    }
