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


def test_file_store_records_structured_repair_gain_from_geometry_evidence(tmp_path):
    from app.evaluation.contracts import EvaluationEvidence, EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.store import FileEvaluationStore

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    store = FileEvaluationStore(tmp_path)
    store.create(session)
    before = EvaluationEvidence(
        evidence_id="geometry-before",
        source_kind="agent_v2.validate_geometry",
        authority="runtime_validation",
        observed={
            "acceptance": {
                "evidence": [
                    {
                        "check_id": "overall-width-y-600",
                        "details": {
                            "comparison": {"value": 600.0, "unit": "mm"}
                        },
                        "measured": [100.0],
                        "outcome": "failed",
                    }
                ]
            }
        },
    )
    after = EvaluationEvidence(
        evidence_id="geometry-after",
        source_kind="agent_v2.validate_geometry",
        authority="runtime_validation",
        observed={
            "acceptance": {
                "evidence": [
                    {
                        "check_id": "overall-width-y-600",
                        "details": {
                            "comparison": {"value": 600.0, "unit": "mm"}
                        },
                        "measured": [600.0],
                        "outcome": "passed",
                    }
                ]
            }
        },
    )
    store.add_evidence(session.evaluation_run_id, before)
    store.append_event(
        session.evaluation_run_id,
        event_type="geometry_validation_finished",
        status="fail",
        evidence_ids=[before.evidence_id],
    )
    store.append_event(
        session.evaluation_run_id,
        event_type="operation_finished",
        status="unknown",
        payload={"activity": "agent_v2.repair_operations", "duration_ms": 1234},
    )
    store.add_evidence(session.evaluation_run_id, after)
    store.append_event(
        session.evaluation_run_id,
        event_type="geometry_validation_finished",
        status="pass",
        evidence_ids=[after.evidence_id],
    )

    store.finish(session.evaluation_run_id, "finished")

    report = json.loads(store.path_for(session.evaluation_run_id).read_text(encoding="utf-8"))
    assert report["repair_gain"] == {
        "status": "measured",
        "repair_count": 1,
        "repair_duration_ms": 1234,
        "before": {
            "evidence_id": "geometry-before",
            "status": "failed",
            "checks": [
                {
                    "check_id": "overall-width-y-600",
                    "target": 600.0,
                    "measured": 100.0,
                    "error": 500.0,
                    "unit": "mm",
                    "outcome": "failed",
                }
            ],
        },
        "after": {
            "evidence_id": "geometry-after",
            "status": "passed",
            "checks": [
                {
                    "check_id": "overall-width-y-600",
                    "target": 600.0,
                    "measured": 600.0,
                    "error": 0.0,
                    "unit": "mm",
                    "outcome": "passed",
                }
            ],
        },
        "improved_checks": ["overall-width-y-600"],
        "regressed_checks": [],
        "remaining_failures": [],
    }


def test_file_store_marks_repair_gain_unknown_without_comparable_geometry_evidence(tmp_path):
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.store import FileEvaluationStore

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    store = FileEvaluationStore(tmp_path)
    store.create(session)
    store.append_event(
        session.evaluation_run_id,
        event_type="operation_finished",
        payload={"activity": "agent_v2.repair_operations", "duration_ms": 1234},
    )

    store.finish(session.evaluation_run_id, "finished")

    report = json.loads(store.path_for(session.evaluation_run_id).read_text(encoding="utf-8"))
    assert report["repair_gain"] == {
        "status": "unknown",
        "repair_count": 1,
        "repair_duration_ms": 1234,
        "reason": "comparable_geometry_evidence_unavailable",
    }


def test_file_store_records_measured_resource_cost_without_inventing_money(tmp_path):
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.store import FileEvaluationStore
    from app.evaluation.writer import human_report_path

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    store = FileEvaluationStore(tmp_path)
    store.create(session)
    store.append_tool_call(
        session.evaluation_run_id,
        {
            "call_id": "requirements-1",
            "call_kind": "workflow_activity",
            "name": "agent_v2.requirements",
            "status": "passed",
            "attempt": 1,
            "replayed": False,
            "duration_ms": 10,
            "model_usage": {
                "unit": "token",
                "input": 100,
                "output": 20,
                "total": 120,
                "reasoning": 5,
            },
        },
    )
    store.append_tool_call(
        session.evaluation_run_id,
        {
            "call_id": "repair-1",
            "call_kind": "workflow_activity",
            "name": "agent_v2.repair_operations",
            "status": "passed",
            "attempt": 1,
            "replayed": False,
            "duration_ms": 30,
        },
    )
    store.append_tool_call(
        session.evaluation_run_id,
        {
            "call_id": "validate-2",
            "call_kind": "workflow_activity",
            "name": "agent_v2.validate_geometry",
            "status": "failed",
            "attempt": 2,
            "replayed": False,
            "duration_ms": 40,
        },
    )
    store.append_tool_call(
        session.evaluation_run_id,
        {
            "call_id": "inspect-1",
            "call_kind": "cad_operation",
            "name": "freecad_inspect",
            "status": "observed",
            "attempt": 1,
            "replayed": False,
        },
    )

    store.finish(session.evaluation_run_id, "finished")

    report = json.loads(store.path_for(session.evaluation_run_id).read_text(encoding="utf-8"))
    cost = report["resource_cost"]
    assert cost["status"] == "partial"
    assert cost["calls"] == {
        "status": "measured",
        "total": 4,
        "by_kind": {"cad_operation": 1, "workflow_activity": 3},
        "failed": 1,
        "retried": 1,
    }
    assert cost["duration"]["status"] == "measured"
    assert cost["duration"]["activity_duration_ms"] == 80
    assert cost["duration"]["by_stage_ms"] == {
        "modeling": 30,
        "requirements": 10,
        "validation": 40,
    }
    assert cost["duration"]["measured_activity_calls"] == 3
    assert cost["duration"]["activity_calls"] == 3
    assert cost["duration"]["coverage_ratio"] == 1.0
    assert cost["duration"]["wall_clock_ms"] >= 0
    assert cost["repair"] == {
        "status": "measured",
        "call_count": 1,
        "duration_ms": 30,
    }
    assert cost["model_usage"] == {
        "status": "partial",
        "unit": "token",
        "input": 100,
        "output": 20,
        "total": 120,
        "reasoning": 5,
        "measured_calls": 1,
        "model_activity_calls": 2,
        "coverage_ratio": 0.5,
    }
    assert cost["monetary_cost"] == {
        "status": "unavailable",
        "reason": "pricing_evidence_unavailable",
    }
    markdown = human_report_path(tmp_path, session.evaluation_run_id).read_text(
        encoding="utf-8"
    )
    assert "## \u8d44\u6e90\u6210\u672c" in markdown
    assert "\u6d3b\u52a8\u7d2f\u8ba1\u8017\u65f6: 80 ms" in markdown
    assert "\u6a21\u578b\u7528\u91cf: \u90e8\u5206\u53ef\u7528" in markdown
    assert "\u8d27\u5e01\u6210\u672c: \u4e0d\u53ef\u7528\uff08\u7f3a\u5c11\u5b9a\u4ef7\u8bc1\u636e\uff09" in markdown


def test_file_store_records_measured_complexity_and_quality_without_guessing(tmp_path):
    from app.evaluation.contracts import EvaluationEvidence, EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.store import FileEvaluationStore

    session = EvaluationSession.create(
        mode=EvaluationMode.OBSERVE,
        source_kind="user_task",
    )
    store = FileEvaluationStore(tmp_path)
    store.create(session)
    store.append_tool_call(
        session.evaluation_run_id,
        {
            "call_id": "cad-1",
            "call_kind": "cad_operation",
            "name": "freecad_execute",
            "status": "observed",
            "attempt": 1,
            "replayed": False,
        },
    )
    store.append_tool_call(
        session.evaluation_run_id,
        {
            "call_id": "activity-1",
            "call_kind": "workflow_activity",
            "name": "agent_v2.validate_geometry",
            "status": "passed",
            "attempt": 1,
            "replayed": False,
            "duration_ms": 20,
        },
    )
    store.add_evidence(
        session.evaluation_run_id,
        EvaluationEvidence(
            evidence_id="geometry-evidence",
            source_kind="agent_v2.validate_geometry",
            authority="runtime_validation",
            observed={
                "acceptance": {
                    "evidence": [
                        {
                            "check_id": "overall-length",
                            "outcome": "passed",
                            "measured": [100.0],
                            "details": {
                                "comparison": {"value": 100.0, "unit": "mm"}
                            },
                        }
                    ]
                },
                "artifacts": [
                    {
                        "filename": "result.step",
                        "dimensions_mm": [100.0, 60.0, 30.0],
                        "solid_count": 1,
                        "volume_mm3": 180000.0,
                    }
                ]
            },
        ),
    )
    store.add_evidence(
        session.evaluation_run_id,
        EvaluationEvidence(
            evidence_id="dfm-evidence",
            source_kind="agent_v2.validate_dfm",
            authority="runtime_validation",
            observed={
                "status": "passed",
                "violations": [],
                "metrics": {"is_watertight": 1, "face_count": 12},
            },
        ),
    )
    store.append_event(
        session.evaluation_run_id,
        event_type="geometry_validation_finished",
        status="pass",
        evidence_ids=["geometry-evidence"],
    )
    store.append_event(
        session.evaluation_run_id,
        event_type="dfm_validation_finished",
        status="pass",
        evidence_ids=["dfm-evidence"],
    )
    store.append_event(
        session.evaluation_run_id,
        event_type="visual_validation_finished",
        status="unknown",
    )
    store.append_event(
        session.evaluation_run_id,
        event_type="revision_committed",
        status="unknown",
    )
    store.append_event(
        session.evaluation_run_id,
        event_type="session_finished",
        status="pass",
        payload={"business_status": "committed"},
    )

    store.finish(session.evaluation_run_id, "finished")

    report = json.loads(store.path_for(session.evaluation_run_id).read_text(encoding="utf-8"))
    assert report["complexity_profile"] == {
        "status": "partial",
        "text": {
            "status": "unavailable",
            "reason": "prompt_complexity_not_recorded",
        },
        "geometry": {
            "status": "measured",
            "dimensions_mm": [100.0, 60.0, 30.0],
            "solid_count": 1,
                "face_count": 12,
                "volume_mm3": 180000.0,
                "artifact_count": 1,
                "evidence_ids": ["dfm-evidence", "geometry-evidence"],
        },
        "operation": {
            "status": "measured",
            "cad_operation_count": 1,
            "workflow_activity_count": 1,
            "repair_count": 0,
            "candidate_count": 0,
            "dependency_depth": None,
        },
        "engineering": {
            "status": "unavailable",
            "reason": "engineering_complexity_not_recorded",
        },
        "input_modality": {
            "status": "unavailable",
            "reason": "input_modality_not_recorded",
        },
    }
    assert report["quality_matrix"] == {
        "status": "partial",
        "geometry": {"status": "passed", "evidence_ids": ["geometry-evidence"]},
        "dfm": {"status": "passed", "evidence_ids": ["dfm-evidence"]},
        "visual": {"status": "unknown", "evidence_ids": []},
        "artifact": {"status": "measured", "artifact_count": 1},
        "lifecycle": {"status": "committed"},
    }
    markdown = store.path_for(session.evaluation_run_id).with_suffix(".md").read_text(
        encoding="utf-8"
    )
    assert "## \u590d\u6742\u5ea6\u4e0e\u8d28\u91cf\u77e9\u9635" in markdown
    assert "- \u51e0\u4f55: measured" in markdown
    assert "- \u8d28\u91cf geometry: passed" in markdown
