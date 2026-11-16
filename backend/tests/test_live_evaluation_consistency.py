import json
import sys
import uuid


def test_normalized_tool_sequence_ignores_runtime_ids_and_timestamps():
    from app.evaluation.consistency import normalize_tool_sequence

    calls = [
        {
            "call_id": str(uuid.uuid4()),
            "call_kind": "workflow_activity",
            "name": "agent_v2.plan",
            "status": "passed",
            "attempt": 1,
            "replayed": False,
            "input_hash": "a" * 64,
            "output_hash": "b" * 64,
            "started_at": "2026-11-07T00:00:00Z",
            "finished_at": "2026-11-07T00:00:01Z",
            "evidence_ids": [str(uuid.uuid4())],
        }
    ]

    normalized = normalize_tool_sequence(calls)

    assert normalized == [
        {
            "call_kind": "workflow_activity",
            "name": "agent_v2.plan",
            "status": "passed",
            "attempt": 1,
            "replayed": False,
            "input_hash": "a" * 64,
            "output_hash": "b" * 64,
        }
    ]


def test_consistency_signature_is_stable_for_same_structure_and_changes_on_reordering():
    from app.evaluation.consistency import summarize_tool_calls

    first = [
        {"call_kind": "workflow_activity", "name": "agent_v2.plan", "status": "passed", "attempt": 1, "replayed": False, "input_hash": "a", "output_hash": "b"},
        {"call_kind": "cad_operation", "name": "freecad_execute", "status": "observed", "attempt": 1, "replayed": False, "input_hash": "c", "output_hash": "d"},
    ]
    equivalent = [
        {**first[0], "call_id": str(uuid.uuid4()), "started_at": "later"},
        {**first[1], "call_id": str(uuid.uuid4()), "finished_at": "later"},
    ]
    reordered = list(reversed(first))

    first_summary = summarize_tool_calls(first)
    equivalent_summary = summarize_tool_calls(equivalent)
    reordered_summary = summarize_tool_calls(reordered)

    assert first_summary["sequence_signature"] == equivalent_summary["sequence_signature"]
    assert first_summary["structural_signature"] == equivalent_summary["structural_signature"]
    assert first_summary["sequence_signature"] != reordered_summary["sequence_signature"]
    assert first_summary["total_call_count"] == 2
    assert first_summary["by_kind"] == {"workflow_activity": 1, "cad_operation": 1}


def test_cross_report_comparison_ignores_runtime_ids_and_duration():
    from app.evaluation.consistency import compare_evaluation_reports

    base = {
        "tool_calls": [
            {
                "call_id": "base-call",
                "call_kind": "workflow_activity",
                "name": "agent_v2.plan",
                "status": "passed",
                "attempt": 1,
                "replayed": False,
                "input_hash": "a" * 64,
                "output_hash": "b" * 64,
                "duration_ms": 100,
            }
        ]
    }
    candidate = {
        "tool_calls": [
            {
                "call_id": "candidate-call",
                "call_kind": "workflow_activity",
                "name": "agent_v2.plan",
                "status": "passed",
                "attempt": 1,
                "replayed": False,
                "input_hash": "a" * 64,
                "output_hash": "b" * 64,
                "duration_ms": 900,
            }
        ]
    }

    comparison = compare_evaluation_reports(base, candidate)

    assert comparison["sequence_match"] is True
    assert comparison["structure_match"] is True
    assert comparison["differences"] == {
        "added": [],
        "removed": [],
        "reordered": False,
        "status_changes": [],
        "input_hash_changes": [],
        "output_hash_changes": [],
    }
    assert comparison["base"]["total_call_count"] == 1
    assert comparison["candidate"]["total_call_count"] == 1


def test_cross_report_comparison_reports_status_and_hash_changes():
    from app.evaluation.consistency import compare_evaluation_reports

    base = {
        "tool_calls": [
            {
                "call_kind": "workflow_activity",
                "name": "agent_v2.execute_freecad",
                "status": "failed",
                "attempt": 1,
                "replayed": False,
                "input_hash": "a" * 64,
                "output_hash": "b" * 64,
            }
        ]
    }
    candidate = {
        "tool_calls": [
            {
                "call_kind": "workflow_activity",
                "name": "agent_v2.execute_freecad",
                "status": "passed",
                "attempt": 1,
                "replayed": False,
                "input_hash": "a" * 64,
                "output_hash": "c" * 64,
            }
        ]
    }

    comparison = compare_evaluation_reports(base, candidate)

    assert comparison["sequence_match"] is False
    assert comparison["structure_match"] is True
    assert comparison["differences"]["status_changes"] == [
        {
            "index": 0,
            "name": "agent_v2.execute_freecad",
            "base": "failed",
            "candidate": "passed",
        }
    ]
    assert comparison["differences"]["output_hash_changes"] == [
        {"index": 0, "name": "agent_v2.execute_freecad"}
    ]


def test_cross_report_comparison_reports_reorder_and_retry_delta():
    from app.evaluation.consistency import compare_evaluation_reports

    base_calls = [
        {
            "call_kind": "workflow_activity",
            "name": "agent_v2.plan",
            "status": "passed",
            "attempt": 1,
            "replayed": False,
            "input_hash": "a",
            "output_hash": "b",
        },
        {
            "call_kind": "workflow_activity",
            "name": "agent_v2.execute_freecad",
            "status": "passed",
            "attempt": 1,
            "replayed": False,
            "input_hash": "c",
            "output_hash": "d",
        },
    ]
    candidate_calls = [
        {**base_calls[1], "attempt": 2},
        base_calls[0],
    ]

    comparison = compare_evaluation_reports(
        {"tool_calls": base_calls},
        {"tool_calls": candidate_calls},
    )

    assert comparison["sequence_match"] is False
    assert comparison["structure_match"] is False
    assert comparison["differences"]["reordered"] is True
    assert comparison["base"]["retry_count"] == 0
    assert comparison["candidate"]["retry_count"] == 1


def test_live_compare_cli_emits_json_without_blocking_on_drift(monkeypatch, tmp_path, capsys):
    from benchmark.live_compare import main

    base_path = tmp_path / "base.json"
    candidate_path = tmp_path / "candidate.json"
    base_path.write_text(
        json.dumps({"tool_calls": [{"name": "agent_v2.plan"}]}),
        encoding="utf-8",
    )
    candidate_path.write_text(
        json.dumps({"tool_calls": [{"name": "agent_v2.execute_freecad"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["live_compare", str(base_path), str(candidate_path), "--json"],
    )

    assert main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["sequence_match"] is False
    assert output["differences"]["added"][0]["name"] == "agent_v2.execute_freecad"
    assert output["conclusion"]["status"] == "different"
    assert output["conclusion"]["reason"] == "tool_path_changed"


def test_live_compare_cli_prints_aggregate_conclusion(monkeypatch, tmp_path, capsys):
    from benchmark.live_compare import main

    base_path = tmp_path / "base.json"
    candidate_path = tmp_path / "candidate.json"
    report = _comparison_report()
    base_path.write_text(json.dumps(report), encoding="utf-8")
    candidate_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["live_compare", str(base_path), str(candidate_path)],
    )

    assert main() == 0

    output = capsys.readouterr().out
    assert "Conclusion: consistent" in output
    assert "Conclusion reason: tool_path_and_outcome_match" in output
    assert "Tool path: consistent" in output
    assert "Outcome: consistent" in output


def test_live_compare_cli_can_fail_on_drift(monkeypatch, tmp_path):
    from benchmark.live_compare import main

    base_path = tmp_path / "base.json"
    candidate_path = tmp_path / "candidate.json"
    base_path.write_text(json.dumps({"tool_calls": []}), encoding="utf-8")
    candidate_path.write_text(
        json.dumps({"tool_calls": [{"name": "agent_v2.plan"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "live_compare",
            str(base_path),
            str(candidate_path),
            "--fail-on-drift",
        ],
    )

    assert main() == 1


def test_live_report_metric_comparison_requires_same_case_identity():
    from app.evaluation.baseline import compare_live_report_metrics

    base = {
        "source": {"kind": "benchmark", "id": "P01"},
        "base_revision_id": "revision-1",
        "complexity_profile": {
            "geometry": {
                "status": "measured",
                "dimensions_mm": [100.0, 60.0, 30.0],
                "solid_count": 1,
                "face_count": 12,
                "volume_mm3": 180000.0,
            },
            "operation": {
                "status": "measured",
                "cad_operation_count": 6,
                "workflow_activity_count": 12,
                "repair_count": 0,
                "candidate_count": 1,
            },
        },
        "quality_matrix": {
            "geometry": {"status": "passed"},
            "dfm": {"status": "passed"},
            "visual": {"status": "unknown"},
            "artifact": {"status": "measured"},
            "lifecycle": {"status": "committed"},
        },
        "resource_cost": {
            "calls": {"total": 19, "failed": 0, "retried": 0},
            "duration": {"wall_clock_ms": 300000, "activity_duration_ms": 75000},
            "model_usage": {"total": 24000},
            "repair": {"duration_ms": None},
        },
    }
    candidate = {
        **base,
        "complexity_profile": {
            **base["complexity_profile"],
            "geometry": {
                **base["complexity_profile"]["geometry"],
                "dimensions_mm": [80.0, 60.0, 30.0],
                "volume_mm3": 144000.0,
            },
            "operation": {
                **base["complexity_profile"]["operation"],
                "cad_operation_count": 5,
                "workflow_activity_count": 11,
            },
        },
        "resource_cost": {
            **base["resource_cost"],
            "calls": {"total": 17, "failed": 0, "retried": 0},
            "duration": {"wall_clock_ms": 180000, "activity_duration_ms": 57000},
            "model_usage": {"total": 24600},
        },
    }

    result = compare_live_report_metrics(base, candidate)

    assert result["status"] == "comparable"
    assert result["identity"] == {
        "source_kind": "benchmark",
        "source_id": "P01",
        "base_revision_match": True,
    }
    assert result["quality"]["geometry"] == {
        "base": "passed",
        "candidate": "passed",
        "changed": False,
    }
    assert result["complexity"]["geometry_dimensions_mm"] == {
        "base": [100.0, 60.0, 30.0],
        "candidate": [80.0, 60.0, 30.0],
        "changed": True,
    }
    assert result["complexity"]["volume_mm3"] == {
        "base": 180000.0,
        "candidate": 144000.0,
        "delta": -36000.0,
    }
    assert result["resource"]["calls_total"] == {
        "base": 19,
        "candidate": 17,
        "delta": -2,
    }
    assert result["resource"]["model_total_tokens"] == {
        "base": 24000,
        "candidate": 24600,
        "delta": 600,
    }


def test_live_report_metric_comparison_marks_mismatched_case_identity_unavailable():
    from app.evaluation.baseline import compare_live_report_metrics

    result = compare_live_report_metrics(
        {"source": {"kind": "user_task", "id": "task-1"}},
        {"source": {"kind": "user_task", "id": "task-2"}},
    )

    assert result == {
        "status": "not_comparable",
        "reason": "source_identity_mismatch",
        "identity": {
            "base_source_kind": "user_task",
            "candidate_source_kind": "user_task",
            "base_source_id": "task-1",
            "candidate_source_id": "task-2",
        },
    }


def test_live_report_metric_comparison_requires_base_revision_identity():
    from app.evaluation.baseline import compare_live_report_metrics

    result = compare_live_report_metrics(
        {"source": {"kind": "benchmark", "id": "P01"}},
        {"source": {"kind": "benchmark", "id": "P01"}},
    )

    assert result == {
        "status": "not_comparable",
        "reason": "base_revision_unavailable",
        "identity": {
            "source_kind": "benchmark",
            "source_id": "P01",
            "base_revision_match": False,
        },
    }


def _comparison_report(*, tool_name="freecad_execute", dimensions=(100.0, 100.0, 100.0)):
    return {
        "source": {"kind": "user_task", "id": str(uuid.uuid4())},
        "tool_calls": [
            {
                "call_kind": "workflow_activity",
                "name": tool_name,
                "status": "passed",
                "attempt": 1,
                "replayed": False,
                "input_hash": "input",
                "output_hash": "output",
            }
        ],
        "complexity_profile": {
            "geometry": {
                "status": "measured",
                "dimensions_mm": list(dimensions),
                "solid_count": 1,
                "face_count": 6,
                "volume_mm3": dimensions[0] * dimensions[1] * dimensions[2],
            }
        },
        "quality_matrix": {
            "geometry": {"status": "passed"},
            "lifecycle": {"status": "committed"},
        },
    }


def test_comparison_conclusion_marks_same_path_with_different_geometry():
    from app.evaluation.consistency import compare_evaluation_reports

    comparison = compare_evaluation_reports(
        _comparison_report(dimensions=(100.0, 100.0, 100.0)),
        _comparison_report(dimensions=(50.0, 50.0, 50.0)),
    )

    assert comparison["structure_match"] is True
    assert comparison["conclusion"] == {
        "status": "different",
        "reason": "geometry_changed",
        "tool_path": "consistent",
        "outcome": "different",
        "geometry": "different",
        "quality": "consistent",
        "lifecycle": "consistent",
        "resource": "not_comparable",
    }


def test_comparison_conclusion_marks_same_geometry_with_different_path():
    from app.evaluation.consistency import compare_evaluation_reports

    comparison = compare_evaluation_reports(
        _comparison_report(tool_name="freecad_execute"),
        _comparison_report(tool_name="freecad_execute_with_repair"),
    )

    assert comparison["conclusion"] == {
        "status": "different",
        "reason": "tool_path_changed",
        "tool_path": "different",
        "outcome": "consistent",
        "geometry": "consistent",
        "quality": "consistent",
        "lifecycle": "consistent",
        "resource": "not_comparable",
    }


def test_comparison_conclusion_allows_different_request_ids_when_evidence_matches():
    from app.evaluation.consistency import compare_evaluation_reports

    base = _comparison_report()
    candidate = _comparison_report()
    candidate["tool_calls"][0]["input_hash"] = "different-input"
    candidate["tool_calls"][0]["output_hash"] = "different-output"

    comparison = compare_evaluation_reports(base, candidate)

    assert comparison["sequence_match"] is False
    assert comparison["structure_match"] is True
    assert comparison["conclusion"]["status"] == "consistent"
    assert comparison["conclusion"]["resource"] == "not_comparable"


def test_comparison_conclusion_distinguishes_missing_evidence():
    from app.evaluation.consistency import compare_evaluation_reports

    comparison = compare_evaluation_reports(
        {"tool_calls": []},
        {"tool_calls": []},
    )

    assert comparison["conclusion"] == {
        "status": "not_comparable",
        "reason": "no_comparable_evidence",
        "tool_path": "unknown",
        "outcome": "unknown",
        "geometry": "unknown",
        "quality": "unknown",
        "lifecycle": "unknown",
        "resource": "not_comparable",
    }


def test_comparison_conclusion_returns_unknown_for_partial_evidence():
    from app.evaluation.consistency import compare_evaluation_reports

    base = _comparison_report()
    candidate = {"tool_calls": base["tool_calls"]}

    comparison = compare_evaluation_reports(base, candidate)

    assert comparison["conclusion"] == {
        "status": "unknown",
        "reason": "incomplete_comparison_evidence",
        "tool_path": "consistent",
        "outcome": "unknown",
        "geometry": "unknown",
        "quality": "unknown",
        "lifecycle": "unknown",
        "resource": "not_comparable",
    }
