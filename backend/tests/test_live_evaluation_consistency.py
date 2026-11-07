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
