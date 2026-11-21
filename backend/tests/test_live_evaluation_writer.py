from __future__ import annotations

import json

import pytest


def test_writer_writes_utf8_report_atomically(tmp_path):
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.writer import report_path, write_session_report

    session = EvaluationSession.create(
        mode=EvaluationMode.OBSERVE,
        source_kind="user_task",
        request_id="request-1",
    )
    session.emit("session_started", payload={"label": "model task"})
    session.finish()

    target = report_path(tmp_path, session.evaluation_run_id)
    written = write_session_report(session, target)

    assert written == target
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "finished"
    assert "model task" in target.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".*.tmp"))


def test_writer_emits_a_human_readable_markdown_report(tmp_path):
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.writer import human_report_path, report_path, write_session_report

    session = EvaluationSession.create(
        mode=EvaluationMode.OBSERVE,
        source_kind="user_task",
        request_id="request-1",
        task_id="task-1",
        workflow_id="workflow-1",
    )
    session.emit("session_started", payload={"objective": "Create a 100 mm cube"})
    session.emit("plan_created", status="pass", payload={"step_key": "plan"})
    session.emit(
        "operation_failed",
        status="error",
        payload={"error_type": "RuntimeError", "error_message": "sandbox stopped"},
    )
    session.finish("failed")

    json_target = report_path(tmp_path, session.evaluation_run_id)
    write_session_report(session, json_target)

    markdown_target = human_report_path(tmp_path, session.evaluation_run_id)
    markdown = markdown_target.read_text(encoding="utf-8")
    assert markdown_target == json_target.with_suffix(".md")
    assert "CAD-Agent \u6267\u884c\u8fc7\u7a0b\u62a5\u544a" in markdown
    assert "\u4efb\u52a1\u76ee\u6807: Create a 100 mm cube" in markdown
    assert "\u6267\u884c\u8ba1\u5212\u5df2\u751f\u6210: \u901a\u8fc7" in markdown
    assert "\u6267\u884c\u5931\u8d25: \u9519\u8bef: RuntimeError: sandbox stopped" in markdown
    assert "\u6700\u7ec8\u72b6\u6001: \u672a\u901a\u8fc7" in markdown


def test_human_report_uses_real_payload_status_when_event_status_is_unknown(tmp_path):
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.writer import human_report_path, report_path, write_session_report

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    session.emit("visual_validation_finished", payload={"status": "rendered"})
    session.emit("change_set_reviewed", payload={"status": "accepted"})
    session.emit("revision_committed", payload={"status": "committed"})
    session.finish()

    target = report_path(tmp_path, session.evaluation_run_id)
    write_session_report(session, target)
    markdown = human_report_path(tmp_path, session.evaluation_run_id).read_text(
        encoding="utf-8"
    )

    assert "\u89c6\u89c9\u6821\u9a8c: \u5df2\u6e32\u67d3" in markdown
    assert "\u53d8\u66f4\u96c6\u5df2\u5ba1\u9605: \u5df2\u63a5\u53d7" in markdown
    assert "\u7248\u672c\u5df2\u63d0\u4ea4: \u5df2\u63d0\u4ea4" in markdown
    assert "\u89c6\u89c9\u6821\u9a8c: \u672a\u786e\u5b9a" not in markdown


def test_human_report_normalizes_runtime_statuses(tmp_path):
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.writer import human_report_path, report_path, write_session_report

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    session.emit("operation_finished", payload={"status": "building"})
    session.emit("operation_finished", status="pass", payload={"status": "succeeded"})
    session.emit("candidate_sealed", status="pass", payload={"status": "succeeded"})
    session.finish()

    target = report_path(tmp_path, session.evaluation_run_id)
    write_session_report(session, target)
    markdown = human_report_path(tmp_path, session.evaluation_run_id).read_text(
        encoding="utf-8"
    )

    assert "\u64cd\u4f5c\u5df2\u5b8c\u6210: \u6267\u884c\u4e2d" in markdown
    assert markdown.count("\u64cd\u4f5c\u5df2\u5b8c\u6210: \u901a\u8fc7") == 1
    assert "\u5019\u9009\u7248\u672c\u5df2\u5c01\u5b58: \u901a\u8fc7" in markdown


def test_human_report_contains_success_engineering_summary(tmp_path):
    from app.evaluation.contracts import EvaluationEvidence, EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.writer import human_report_path, report_path, write_session_report

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    session.emit("session_started", payload={"objective": "Create a cube"})
    session.emit("requirement_parsed")
    session.add_evidence(
        EvaluationEvidence(
            evidence_id="geometry-evidence",
            source_kind="agent_v2.validate_geometry",
        )
    )
    session.add_evidence(
        EvaluationEvidence(
            evidence_id="dfm-evidence",
            source_kind="agent_v2.validate_dfm",
        )
    )
    session.emit(
        "geometry_validation_finished",
        status="pass",
        evidence_ids=["geometry-evidence"],
        payload={"outcome": "passed", "status": "completed"},
    )
    session.emit("visual_validation_finished", payload={"status": "rendered"})
    session.emit(
        "dfm_validation_finished",
        status="pass",
        evidence_ids=["dfm-evidence"],
        payload={"outcome": "passed", "status": "completed"},
    )
    session.emit("candidate_sealed", status="pass", payload={"status": "succeeded"})
    session.emit("change_set_reviewed", payload={"status": "accepted"})
    session.emit("revision_committed", payload={"status": "committed"})
    session.finish()

    target = report_path(tmp_path, session.evaluation_run_id)
    write_session_report(session, target)
    markdown = human_report_path(tmp_path, session.evaluation_run_id).read_text(
        encoding="utf-8"
    )

    assert "## \u5de5\u7a0b\u8fc7\u7a0b\u7ed3\u8bba" in markdown
    assert "- \u9700\u6c42\u89e3\u6790: \u5df2\u8bb0\u5f55" in markdown
    assert "- \u51e0\u4f55\u7ed3\u679c: \u901a\u8fc7" in markdown
    assert "- \u89c6\u89c9\u7ed3\u679c: \u5df2\u6e32\u67d3" in markdown
    assert "- \u5236\u9020\u6027\u7ed3\u679c: \u901a\u8fc7" in markdown
    assert "- \u662f\u5426\u53d1\u751f\u4fee\u590d: \u672a\u786e\u5b9a" in markdown
    assert "- \u5019\u9009\u7248\u672c: \u5df2\u5c01\u5b58" in markdown
    assert "- \u53d8\u66f4\u5ba1\u9605: \u5df2\u63a5\u53d7" in markdown
    assert "- \u7248\u672c\u63d0\u4ea4: \u5df2\u5b8c\u6210" in markdown
    assert "- \u6700\u7ec8\u7ed3\u8bba: \u4efb\u52a1\u5b8c\u6210" in markdown


def test_human_report_contains_blocked_engineering_summary(tmp_path):
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.writer import human_report_path, report_path, write_session_report

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    session.emit("session_started", payload={"objective": "Modify a part"})
    session.emit(
        "operation_failed",
        status="error",
        payload={
            "activity": "agent_v2.requirements",
            "error_type": "ApplicationError",
            "error_message": "engineering_verification_unavailable: missing binding",
        },
    )
    session.emit("session_finished", status="fail", payload={"business_status": "failed"})
    session.finish("failed")

    target = report_path(tmp_path, session.evaluation_run_id)
    write_session_report(session, target)
    markdown = human_report_path(tmp_path, session.evaluation_run_id).read_text(
        encoding="utf-8"
    )

    assert "## \u5de5\u7a0b\u8fc7\u7a0b\u7ed3\u8bba" in markdown
    assert "- \u9700\u6c42\u89e3\u6790: \u5931\u8d25" in markdown
    assert "- \u5931\u8d25\u9636\u6bb5: \u9700\u6c42\u6838\u9a8c" in markdown
    assert "- \u5931\u8d25\u7c7b\u578b: \u5de5\u7a0b\u6761\u4ef6\u65e0\u6cd5\u53ef\u9760\u6838\u9a8c" in markdown
    assert "- \u662f\u5426\u8fdb\u5165\u5efa\u6a21: \u5426" in markdown
    assert "- \u6700\u7ec8\u7ed3\u8bba: \u4efb\u52a1\u672a\u5b8c\u6210" in markdown


def test_human_report_does_not_treat_terminal_failure_as_success_path(tmp_path):
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.writer import human_report_path, report_path, write_session_report

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    session.emit("session_finished", status="fail", payload={"business_status": "failed"})
    session.finish("failed")

    target = report_path(tmp_path, session.evaluation_run_id)
    write_session_report(session, target)
    markdown = human_report_path(tmp_path, session.evaluation_run_id).read_text(
        encoding="utf-8"
    )

    assert "- \u5931\u8d25\u9636\u6bb5: \u6267\u884c\u9636\u6bb5" in markdown
    assert "- \u5931\u8d25\u7c7b\u578b: \u5de5\u4f5c\u6d41\u672a\u5b8c\u6210" in markdown
    assert "- \u662f\u5426\u8fdb\u5165\u5efa\u6a21: \u672a\u786e\u5b9a" in markdown
    assert "- \u6700\u7ec8\u7ed3\u8bba: \u4efb\u52a1\u672a\u5b8c\u6210" in markdown


def test_human_report_includes_real_quantitative_geometry_and_dfm_evidence():
    from app.evaluation.human_report import render_human_report

    report = {
        "evaluation_run_id": "run-quantified",
        "workflow_id": "workflow-quantified",
        "status": "finished",
        "source": {"kind": "user_task"},
        "timeline": [
            {
                "event_type": "geometry_validation_finished",
                "status": "fail",
                "evidence_ids": ["geometry-before"],
                "payload": {"activity": "agent_v2.validate_geometry"},
            },
            {
                "event_type": "operation_finished",
                "status": "unknown",
                "evidence_ids": [],
                "payload": {"activity": "agent_v2.repair_operations"},
            },
            {
                "event_type": "geometry_validation_finished",
                "status": "pass",
                "evidence_ids": ["geometry-after"],
                "payload": {"activity": "agent_v2.validate_geometry"},
            },
            {
                "event_type": "dfm_validation_finished",
                "status": "pass",
                "evidence_ids": ["dfm-after"],
                "payload": {"activity": "agent_v2.validate_dfm"},
            },
        ],
        "evidence": [
            {
                "evidence_id": "geometry-before",
                "source_kind": "agent_v2.validate_geometry",
                "observed": {
                    "outcome": "failed",
                    "dimension_tolerance": 0.05,
                    "acceptance": {
                        "evidence": [
                            {
                                "check_id": "width-y-eq-200",
                                "details": {
                                    "comparison": {"value": 200.0, "unit": "mm"},
                                    "measured": [100.0],
                                },
                                "outcome": "failed",
                            }
                        ]
                    },
                    "artifacts": [
                        {
                            "filename": "geometry-before.step",
                            "dimensions_mm": [100.0, 100.0, 100.0],
                            "solid_count": 1,
                            "volume_mm3": 1000000.0,
                        }
                    ],
                },
            },
            {
                "evidence_id": "geometry-after",
                "source_kind": "agent_v2.validate_geometry",
                "observed": {
                    "outcome": "passed",
                    "dimension_tolerance": 0.05,
                    "acceptance": {
                        "evidence": [
                            {
                                "check_id": "width-y-eq-200",
                                "details": {
                                    "comparison": {"value": 200.0, "unit": "mm"},
                                    "measured": [200.0],
                                },
                                "outcome": "passed",
                            }
                        ]
                    },
                    "artifacts": [
                        {
                            "filename": "geometry-after.step",
                            "dimensions_mm": [100.0, 200.0, 100.0],
                            "solid_count": 1,
                            "volume_mm3": 2000000.0,
                        }
                    ],
                },
            },
            {
                "evidence_id": "dfm-after",
                "source_kind": "agent_v2.validate_dfm",
                "observed": {
                    "outcome": "passed",
                    "process": "FDM",
                    "metrics": {
                        "min_wall_thickness_mm": 100.0,
                        "overhang_ratio": 0.0,
                    },
                    "evaluated_rule_ids": ["fdm_wall_thickness", "fdm_overhang"],
                    "violations": [],
                    "policy_object": {
                        "rules": [
                            {
                                "id": "fdm_wall_thickness",
                                "threshold_min": 0.8,
                                "unit": "mm",
                            },
                            {
                                "id": "fdm_overhang",
                                "threshold_max": 0.3,
                                "unit": "ratio",
                            },
                        ]
                    },
                },
            },
        ],
    }

    markdown = render_human_report(report)

    assert "## \u51e0\u4f55\u91cf\u5316\u8bc4\u6d4b" in markdown
    assert "\u76ee\u6807 200 mm; \u5b9e\u6d4b 100 mm" in markdown
    assert "\u76ee\u6807 200 mm; \u5b9e\u6d4b 200 mm" in markdown
    assert "\u5c3a\u5bf8 100 x 200 x 100 mm" in markdown
    assert "\u4f53\u79ef 2000000 mm^3" in markdown
    assert "## DFM \u91cf\u5316\u8bc4\u6d4b" in markdown
    assert "\u8fc7\u7a0b: FDM" in markdown
    assert "min_wall_thickness_mm: 100 mm" in markdown
    assert "\u8fdd\u89c4: 0" in markdown
    assert "\u89c4\u5219 fdm_wall_thickness: \u5df2\u8bc4\u6d4b; \u9608\u503c >= 0.8 mm" in markdown
    assert "\u89c4\u5219 fdm_overhang: \u5df2\u8bc4\u6d4b; \u9608\u503c <= 0.3 ratio" in markdown
    assert "## \u4fee\u590d\u524d\u540e\u5bf9\u6bd4" in markdown
    assert "\u4fee\u590d\u6d3b\u52a8: agent_v2.repair_operations" in markdown


def test_human_report_marks_missing_quantitative_fields_as_unrecorded():
    from app.evaluation.human_report import render_human_report

    markdown = render_human_report(
        {
            "evaluation_run_id": "run-no-metrics",
            "status": "finished",
            "source": {"kind": "user_task"},
            "timeline": [
                {
                    "event_type": "geometry_validation_finished",
                    "status": "pass",
                    "evidence_ids": ["geometry-empty"],
                    "payload": {},
                }
            ],
            "evidence": [
                {
                    "evidence_id": "geometry-empty",
                    "source_kind": "agent_v2.validate_geometry",
                    "observed": {"outcome": "passed"},
                }
            ],
        }
    )

    assert "\u5b9e\u6d4b\u503c: \u672a\u8bb0\u5f55" in markdown
    assert "\u6ca1\u6709\u53ef\u5c55\u793a\u7684 DFM \u91cf\u5316\u8bc1\u636e" in markdown


def test_human_report_shows_stage_duration_and_real_repair_benefit():
    from app.evaluation.human_report import render_human_report

    report = {
        "evaluation_run_id": "run-duration-repair",
        "status": "finished",
        "source": {"kind": "user_task"},
        "timeline": [
            {
                "event_type": "geometry_validation_finished",
                "status": "fail",
                "evidence_ids": ["before"],
                "payload": {
                    "activity": "agent_v2.validate_geometry",
                    "duration_ms": 120,
                },
            },
            {
                "event_type": "operation_finished",
                "status": "pass",
                "evidence_ids": [],
                "payload": {
                    "activity": "agent_v2.repair_operations",
                    "duration_ms": 340,
                },
            },
            {
                "event_type": "geometry_validation_finished",
                "status": "pass",
                "evidence_ids": ["after"],
                "payload": {
                    "activity": "agent_v2.validate_geometry",
                    "duration_ms": 180,
                },
            },
        ],
        "evidence": [
            {
                "evidence_id": "before",
                "source_kind": "agent_v2.validate_geometry",
                "observed": {
                    "outcome": "failed",
                    "acceptance": {
                        "evidence": [
                            {
                                "check_id": "width-y-eq-200",
                                "details": {
                                    "comparison": {"value": 200.0, "unit": "mm"},
                                    "measured": [100.0],
                                },
                                "outcome": "failed",
                            }
                        ]
                    },
                },
            },
            {
                "evidence_id": "after",
                "source_kind": "agent_v2.validate_geometry",
                "observed": {
                    "outcome": "passed",
                    "acceptance": {
                        "evidence": [
                            {
                                "check_id": "width-y-eq-200",
                                "details": {
                                    "comparison": {"value": 200.0, "unit": "mm"},
                                    "measured": [200.0],
                                },
                                "outcome": "passed",
                            }
                        ]
                    },
                },
            },
        ],
        "metrics": {"total_duration_ms": 640},
    }

    markdown = render_human_report(report)

    assert "## \u9636\u6bb5\u8017\u65f6" in markdown
    assert "agent_v2.repair_operations: 340 ms" in markdown
    assert "\u603b\u8017\u65f6: 640 ms" in markdown
    assert "\u4fee\u590d\u524d: \u76ee\u6807 200 mm; \u5b9e\u6d4b 100 mm; \u8bef\u5dee 100 mm" in markdown
    assert "\u4fee\u590d\u540e: \u76ee\u6807 200 mm; \u5b9e\u6d4b 200 mm; \u8bef\u5dee 0 mm" in markdown


def test_human_report_shows_tool_call_statistics_and_consistency():
    from app.evaluation.human_report import render_human_report

    markdown = render_human_report({
        "evaluation_run_id": "run-tool-summary",
        "status": "finished",
        "source": {"kind": "user_task"},
        "timeline": [],
        "tool_calls": [
            {
                "call_kind": "workflow_activity",
                "name": "agent_v2.plan",
                "status": "passed",
                "attempt": 1,
                "replayed": False,
                "input_hash": "a" * 64,
                "output_hash": "b" * 64,
            },
            {
                "call_kind": "cad_operation",
                "name": "freecad_execute",
                "status": "observed",
                "attempt": 1,
                "replayed": False,
                "input_hash": "c" * 64,
                "output_hash": "d" * 64,
            },
        ],
        "metrics": {
            "total_call_count": 2,
            "activity_count": 1,
            "cad_operation_count": 1,
            "failed_call_count": 0,
        },
        "consistency": {
            "sequence_signature": "e" * 64,
            "structural_signature": "f" * 64,
        },
    })

    assert "## \u5de5\u5177\u8c03\u7528\u7edf\u8ba1" in markdown
    assert "\u603b\u8c03\u7528: 2" in markdown
    assert "workflow_activity: 1" in markdown
    assert "cad_operation: 1" in markdown
    assert "## \u8c03\u7528\u5e8f\u5217" in markdown
    assert "1. workflow_activity / agent_v2.plan / passed" in markdown
    assert "2. cad_operation / freecad_execute / observed" in markdown
    assert "## \u8c03\u7528\u4e00\u81f4\u6027" in markdown
    assert "\u8c03\u7528\u5e8f\u5217\u7b7e\u540d: " + "e" * 64 in markdown


def test_writer_does_not_write_artifact_bytes(tmp_path):
    from app.evaluation.contracts import EvaluationEvidence, EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.writer import write_session_report

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    session.add_evidence(
        EvaluationEvidence(
            evidence_id="artifact-1",
            source_kind="artifact",
            source_ref="object-key",
            artifact_sha256="a" * 64,
            observed={"size_bytes": 10},
        )
    )
    session.finish()

    target = tmp_path / "report.json"
    write_session_report(session, target)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["evidence"][0]["artifact_sha256"] == "a" * 64
    assert "bytes" not in payload["evidence"][0]
    assert "content" not in payload["evidence"][0]


def test_writer_failure_is_recorded_without_replacing_business_result(monkeypatch, tmp_path):
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.writer import safe_write_session_report

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    session.finish()

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr("app.evaluation.writer.write_session_report", fail)
    business_result = {"status": "succeeded", "revision_id": "revision-1"}

    assert safe_write_session_report(session, tmp_path / "report.json") is None
    assert business_result == {"status": "succeeded", "revision_id": "revision-1"}
    assert session.to_report()["errors"] == ["evaluation report write failed: disk unavailable"]


def test_writer_rejects_secret_in_report_payload(tmp_path):
    from app.evaluation.contracts import EvaluationMode
    from app.evaluation.session import EvaluationSession
    from app.evaluation.writer import write_session_report

    session = EvaluationSession.create(mode=EvaluationMode.OBSERVE)
    with pytest.raises(ValueError, match="sensitive"):
        session.emit("session_started", payload={"provider_secret": "secret"})

    session.finish()
    write_session_report(session, tmp_path / "report.json")
