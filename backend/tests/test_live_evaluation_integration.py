from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_model_operation_observer_persists_measured_duration_in_report(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation import integration as integration_module
    from app.evaluation.integration import (
        evaluation_run_id_for_workflow,
        observe_model_operation,
    )
    from app.evaluation.store import FileEvaluationStore

    clock = iter((30.0, 30.75))
    monkeypatch.setattr(integration_module, "default_report_root", lambda: tmp_path)
    monkeypatch.setattr(
        integration_module.time,
        "perf_counter",
        lambda: next(clock),
    )

    async def operation(_payload):
        return {"status": "completed", "step_key": "generate-source"}

    result = await observe_model_operation(
        "agent_v2.generate_source",
        operation,
    )(
        {
            "workflow_run_id": "workflow-observer-report",
            "objective": "create a cube",
        }
    )
    assert result == {"status": "completed", "step_key": "generate-source"}
    run_id = evaluation_run_id_for_workflow("workflow-observer-report")

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    call = report["tool_calls"][0]
    assert call["name"] == "agent_v2.generate_source"
    assert call["duration_ms"] == 750
    assert call["started_at"]
    assert call["finished_at"]
    assert report["metrics"]["total_duration_ms"] == 750


def test_disabled_mode_does_not_create_live_session(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "disabled")

    from app.evaluation.integration import ensure_live_session

    assert ensure_live_session(
        {"workflow_run_id": "workflow-1", "objective": "生成模型"},
        root=tmp_path,
    ) is None


def test_observe_session_needs_only_the_real_workflow_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import ensure_live_session

    run_id = ensure_live_session(
        {
            "workflow_run_id": "workflow-without-document-id",
            "objective": "create a cube",
        },
        root=tmp_path,
    )

    assert run_id == "live-workflow-without-document-id"
    assert (tmp_path / "live" / "live_live-workflow-without-document-id.json").exists()
    assert (tmp_path / "live" / "live_live-workflow-without-document-id.md").exists()


def test_observe_mode_uses_one_report_for_api_and_worker_instances(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import (
        ensure_live_session,
        record_activity_result,
        evaluation_run_id_for_workflow,
    )
    from app.evaluation.store import FileEvaluationStore

    payload = {
        "workflow_run_id": "workflow-1",
        "operation": "generate",
        "objective": "生成一个边长 10 mm 的正方体",
    }
    run_id = ensure_live_session(payload, root=tmp_path)
    assert run_id == evaluation_run_id_for_workflow("workflow-1")

    record_activity_result(
        payload,
        "agent_v2.validate_geometry",
        {
            "status": "completed",
            "gate": "geometry",
            "outcome": "passed",
            "evidence_id": "evidence-1",
            "evidence_hash": "a" * 64,
            "report": {"valid_solid": True, "bbox_mm": [10.0, 10.0, 10.0]},
        },
        root=tmp_path,
        event_id="workflow-1:validate-geometry:1",
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    assert [event["event_type"] for event in report["timeline"]] == [
        "session_started",
        "geometry_validation_finished",
    ]
    assert report["timeline"][1]["status"] == "pass"
    assert report["timeline"][1]["evidence_ids"] == ["evidence-1"]
    assert report["timeline"][1]["payload"]["activity"] == "agent_v2.validate_geometry"
    assert report["evidence"][0]["artifact_sha256"] == "a" * 64


def test_activity_recording_persists_measured_duration_and_updates_metrics(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import ensure_live_session, record_activity_result
    from app.evaluation.store import FileEvaluationStore

    payload = {"workflow_run_id": "workflow-duration", "objective": "create a cube"}
    run_id = ensure_live_session(payload, root=tmp_path)

    record_activity_result(
        payload,
        "agent_v2.plan",
        {"status": "completed", "step_key": "plan"},
        root=tmp_path,
        started_at="2026-11-07T00:00:00Z",
        finished_at="2026-11-07T00:00:01.250Z",
        duration_ms=1250,
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    event = report["timeline"][-1]
    assert event["payload"]["started_at"] == "2026-11-07T00:00:00Z"
    assert event["payload"]["finished_at"] == "2026-11-07T00:00:01.250Z"
    assert event["payload"]["duration_ms"] == 1250
    assert report["metrics"]["total_duration_ms"] == 1250


def test_activity_failure_persists_measured_duration(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import record_activity_failure
    from app.evaluation.store import FileEvaluationStore

    run_id = record_activity_failure(
        {"workflow_run_id": "workflow-failure-duration", "objective": "create a cube"},
        "agent_v2.execute_freecad",
        RuntimeError("FreeCAD failed"),
        root=tmp_path,
        started_at="2026-11-07T00:00:00Z",
        finished_at="2026-11-07T00:00:00.500Z",
        duration_ms=500,
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    event = report["timeline"][-1]
    assert event["payload"]["duration_ms"] == 500
    assert report["metrics"]["total_duration_ms"] == 500


def test_activity_recording_creates_a_safe_workflow_tool_call(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import ensure_live_session, record_activity_result
    from app.evaluation.store import FileEvaluationStore

    payload = {
        "workflow_run_id": "workflow-tool-call",
        "step_key": "geometry",
        "attempt_id": "attempt-1",
        "objective": "create a cube",
    }
    run_id = ensure_live_session(payload, root=tmp_path)
    record_activity_result(
        payload,
        "agent_v2.validate_geometry",
        {
            "status": "completed",
            "outcome": "passed",
            "evidence_id": "geometry-evidence",
            "report": {"valid_solid": True},
        },
        root=tmp_path,
        event_id="workflow-tool-call:geometry:1",
        started_at="2026-11-07T00:00:00Z",
        finished_at="2026-11-07T00:00:00.250Z",
        duration_ms=250,
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    call = report["tool_calls"][0]
    assert call["call_kind"] == "workflow_activity"
    assert call["name"] == "agent_v2.validate_geometry"
    assert call["sequence"] == 1
    assert call["status"] == "passed"
    assert call["duration_ms"] == 250
    assert call["input_hash"]
    assert call["output_hash"]
    assert call["evidence_ids"] == ["geometry-evidence"]
    assert "objective" not in call
    assert report["metrics"]["activity_count"] == 1


def test_activity_tool_call_normalizes_lifecycle_statuses(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import ensure_live_session, record_activity_result
    from app.evaluation.store import FileEvaluationStore

    payload = {"workflow_run_id": "workflow-status-normalization"}
    run_id = ensure_live_session(payload, root=tmp_path)
    record_activity_result(
        payload,
        "agent_v2.allocate_candidate",
        {"status": "building"},
        root=tmp_path,
        event_id="workflow-status-normalization:allocate",
    )
    record_activity_result(
        payload,
        "agent_v2.render_visual",
        {"status": "rendered"},
        root=tmp_path,
        event_id="workflow-status-normalization:render",
    )
    record_activity_result(
        payload,
        "agent_v2.requirements",
        {},
        root=tmp_path,
        event_id="workflow-status-normalization:requirements",
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    assert [call["status"] for call in report["tool_calls"]] == [
        "running",
        "observed",
        "unknown",
    ]


def test_failed_activity_creates_an_error_workflow_tool_call(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import record_activity_failure
    from app.evaluation.store import FileEvaluationStore

    run_id = record_activity_failure(
        {"workflow_run_id": "workflow-tool-failure", "step_key": "execute"},
        "agent_v2.execute_freecad",
        RuntimeError("backend unavailable"),
        root=tmp_path,
        duration_ms=80,
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    call = report["tool_calls"][0]
    assert call["call_kind"] == "workflow_activity"
    assert call["name"] == "agent_v2.execute_freecad"
    assert call["status"] == "error"
    assert call["duration_ms"] == 80
    assert report["metrics"]["failed_call_count"] == 1


def test_activity_recording_projects_real_model_and_cad_tool_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import ensure_live_session, record_activity_result
    from app.evaluation.store import FileEvaluationStore

    payload = {"workflow_run_id": "workflow-nested-tools", "step_key": "operations"}
    run_id = ensure_live_session(payload, root=tmp_path)
    record_activity_result(
        payload,
        "agent_v2.generate_operations",
        {
            "status": "completed",
            "provenance": {
                "tool_calls": [
                    {
                        "call": {
                            "id": "tool-1",
                            "function": {
                                "name": "freecad_inspect",
                                "arguments": '{"object":"Pad"}',
                            },
                        },
                        "result": {"objects": ["Pad"]},
                    },
                    {
                        "name": "submit_requirements",
                        "arguments_hash": "a" * 64,
                    },
                ],
                "execution_tool_call": {
                    "id": "tool-2",
                    "function": {
                        "name": "freecad_execute",
                        "arguments": '{"operations":[{"action":"box"}]}',
                    },
                },
            },
        },
        root=tmp_path,
        event_id="workflow-nested-tools:operations:1",
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    calls = report["tool_calls"]
    assert [call["call_kind"] for call in calls] == [
        "workflow_activity",
        "cad_operation",
        "model_tool",
        "cad_operation",
    ]
    assert [call["name"] for call in calls[1:]] == [
        "freecad_inspect",
        "submit_requirements",
        "freecad_execute",
    ]
    assert all("arguments" not in call for call in calls)
    assert report["metrics"]["model_tool_count"] == 1
    assert report["metrics"]["cad_operation_count"] == 2
    assert report["metrics"]["total_call_count"] == 4
    assert len(report["consistency"]["sequence_signature"]) == 64
    assert report["consistency"]["total_call_count"] == 4


def test_activity_recording_uses_real_provider_tool_call_receipt(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import ensure_live_session, record_activity_result
    from app.evaluation.store import FileEvaluationStore

    monkeypatch.setattr(
        "app.llm.get_last_chat_completion_provenance",
        lambda: {
            "tool_calls": [{
                "name": "submit_requirements",
                "arguments_hash": "a" * 64,
            }]
        },
    )
    payload = {"workflow_run_id": "workflow-provider-tool", "step_key": "requirements"}
    run_id = ensure_live_session(payload, root=tmp_path)
    record_activity_result(
        payload,
        "agent_v2.requirements",
        {"status": "completed"},
        root=tmp_path,
        event_id="workflow-provider-tool:requirements:1",
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    assert [call["name"] for call in report["tool_calls"]] == [
        "agent_v2.requirements",
        "submit_requirements",
    ]
    assert report["tool_calls"][1]["call_kind"] == "model_tool"


def test_non_model_activity_does_not_reuse_stale_provider_tool_receipt(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import ensure_live_session, record_activity_result
    from app.evaluation.store import FileEvaluationStore

    monkeypatch.setattr(
        "app.llm.get_last_chat_completion_provenance",
        lambda: {"tool_calls": [{"name": "stale_tool", "arguments_hash": "a" * 64}]},
    )
    payload = {"workflow_run_id": "workflow-stale-provider", "step_key": "geometry"}
    run_id = ensure_live_session(payload, root=tmp_path)
    record_activity_result(
        payload,
        "agent_v2.validate_geometry",
        {"status": "completed"},
        root=tmp_path,
        event_id="workflow-stale-provider:geometry:1",
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    assert [call["name"] for call in report["tool_calls"]] == [
        "agent_v2.validate_geometry"
    ]


def test_activity_recording_failure_does_not_raise_to_business_path(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation import integration

    payload = {"workflow_run_id": "workflow-2", "objective": "生成模型"}
    assert integration.ensure_live_session(payload, root=tmp_path) == "live-workflow-2"

    def fail(*args, **kwargs):
        raise OSError("report disk unavailable")

    monkeypatch.setattr(integration.FileEvaluationStore, "append_event", fail)

    result = integration.record_activity_result(
        payload,
        "agent_v2.plan",
        {"status": "completed", "step_key": "plan"},
        root=tmp_path,
    )

    assert result == "live-workflow-2"


def test_activity_failure_records_safe_error_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import record_activity_failure
    from app.evaluation.store import FileEvaluationStore

    run_id = record_activity_failure(
        {"workflow_run_id": "workflow-3", "objective": "生成模型"},
        "agent_v2.execute_freecad",
        RuntimeError("FreeCAD failed with provider token redacted"),
        root=tmp_path,
        event_id="workflow-3:execute:1",
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    event = report["timeline"][-1]
    assert event["event_type"] == "operation_failed"
    assert event["status"] == "error"
    assert event["payload"]["error_type"] == "RuntimeError"


@pytest.mark.parametrize(
    ("business_status", "evaluation_status"),
    [
        ("succeeded", "finished"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("timed_out", "failed"),
    ],
)
def test_terminal_activity_result_finishes_report(monkeypatch, tmp_path, business_status, evaluation_status):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import (
        ensure_live_session,
        record_workflow_terminal,
    )
    from app.evaluation.store import FileEvaluationStore

    payload = {"workflow_run_id": "workflow-terminal", "objective": "create a cube"}
    run_id = ensure_live_session(payload, root=tmp_path)
    assert run_id == "live-workflow-terminal"

    record_workflow_terminal(
        payload,
        {"status": business_status},
        root=tmp_path,
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    assert report["status"] == evaluation_status
    assert report["finished_at"]
    assert report["timeline"][-1]["event_type"] == "session_finished"
    assert report["timeline"][-1]["payload"]["business_status"] == business_status


def test_terminal_report_failure_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation import integration

    payload = {"workflow_run_id": "workflow-terminal-failure", "objective": "create a cube"}
    run_id = integration.ensure_live_session(payload, root=tmp_path)

    def fail(*args, **kwargs):
        raise OSError("report disk unavailable")

    monkeypatch.setattr(integration.FileEvaluationStore, "finish", fail)

    assert integration.record_workflow_terminal(
        payload,
        {"status": "succeeded"},
        root=tmp_path,
    ) == run_id


def test_activity_recording_links_authoritative_ids_from_activity_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import ensure_live_session, record_activity_result
    from app.evaluation.store import FileEvaluationStore

    payload = {
        "workflow_run_id": "workflow-identities",
        "objective": "modify a part",
        "candidate_build_id": "candidate-1",
        "change_set_id": "change-1",
    }
    run_id = ensure_live_session(payload, root=tmp_path)

    record_activity_result(
        payload,
        "agent_v2.seal_candidate",
        {"status": "succeeded", "candidate_revision_id": "revision-1"},
        root=tmp_path,
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    assert report["candidate_build_id"] == "candidate-1"
    assert report["candidate_revision_id"] == "revision-1"
    assert report["change_set_id"] == "change-1"


def test_history_restore_admission_records_source_and_base_revision_without_claiming_commit(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import ensure_live_session
    from app.evaluation.store import FileEvaluationStore

    payload = {
        "workflow_run_id": "workflow-restore",
        "operation": "modify",
        "objective": "restore historical revision",
        "expected_base_revision_id": "head-revision-2",
        "revision_restore": {"source_revision_id": "historical-revision-1"},
        "operation_context": {"rule": "explicit_history_restore"},
    }
    run_id = ensure_live_session(payload, root=tmp_path)

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    assert report["base_revision_id"] == "head-revision-2"
    assert report["source_revision_id"] == "historical-revision-1"
    event = report["timeline"][0]
    assert event["event_type"] == "session_started"
    assert event["payload"]["operation"] == "modify"
    assert event["payload"]["restore_source_revision_id"] == "historical-revision-1"
    assert event["payload"]["restore_requested"] is True
    assert "committed" not in event["payload"]


@pytest.mark.parametrize(
    ("result", "event_type"),
    [
        ({"status": "succeeded", "committed": False}, "change_set_created"),
        ({"status": "succeeded", "committed": True}, "revision_committed"),
    ],
)
def test_finalize_activity_event_reflects_authoritative_change_set_transition(
    monkeypatch, tmp_path, result, event_type
):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import ensure_live_session, record_activity_result
    from app.evaluation.store import FileEvaluationStore

    payload = {
        "workflow_run_id": "workflow-change-set",
        "objective": "modify a part",
        "change_set_id": "change-2",
    }
    run_id = ensure_live_session(payload, root=tmp_path)
    record_activity_result(payload, "mcad.finalize", result, root=tmp_path)

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    assert report["timeline"][-1]["event_type"] == event_type


@pytest.mark.parametrize(
    ("action", "event_type"),
    [
        ("accepted", "change_set_reviewed"),
        ("committed", "revision_committed"),
        ("rejected", "change_set_rejected"),
        ("changes_requested", "change_set_reviewed"),
        ("rolled_back", "revision_rolled_back"),
    ],
)
def test_change_set_transition_records_authoritative_service_result(
    monkeypatch, tmp_path, action, event_type
):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import ensure_live_session, record_change_set_transition
    from app.evaluation.store import FileEvaluationStore

    payload = {"workflow_run_id": "workflow-change-service", "objective": "modify a part"}
    run_id = ensure_live_session(payload, root=tmp_path)
    record_change_set_transition(
        payload,
        action,
        {"change_set_id": "change-service-1", "status": action},
        root=tmp_path,
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    event = next(
        item for item in report["timeline"] if item["event_type"] == event_type
    )
    assert event["event_type"] == event_type
    assert event["payload"]["status"] == action
    assert report["change_set_id"] == "change-service-1"


@pytest.mark.parametrize(
    "action",
    ["committed", "rejected", "changes_requested"],
)
def test_terminal_change_set_transition_finishes_live_report(
    monkeypatch, tmp_path, action
):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import ensure_live_session, record_change_set_transition
    from app.evaluation.store import FileEvaluationStore

    payload = {
        "workflow_run_id": "workflow-terminal-change",
        "objective": "modify a part",
    }
    run_id = ensure_live_session(payload, root=tmp_path)

    record_change_set_transition(
        payload,
        action,
        {"change_set_id": "change-terminal-1", "status": action},
        root=tmp_path,
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    assert report["status"] == "finished"
    assert report["finished_at"]
    assert report["timeline"][-1]["event_type"] == "session_finished"
    assert report["timeline"][-1]["payload"]["business_status"] == action


def test_change_set_acceptance_keeps_live_report_open(monkeypatch, tmp_path):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    from app.evaluation.integration import ensure_live_session, record_change_set_transition
    from app.evaluation.store import FileEvaluationStore

    payload = {
        "workflow_run_id": "workflow-accepted-change",
        "objective": "modify a part",
    }
    run_id = ensure_live_session(payload, root=tmp_path)

    record_change_set_transition(
        payload,
        "accepted",
        {"change_set_id": "change-accepted-1", "status": "accepted"},
        root=tmp_path,
    )

    report = json.loads(
        FileEvaluationStore(tmp_path).path_for(run_id).read_text(encoding="utf-8")
    )
    assert report["status"] == "running"
    assert report["finished_at"] is None
