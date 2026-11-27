from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import subprocess


class _RouteDataset:
    def __init__(self, cases):
        self.cases = tuple(cases)
        self.case_ids = frozenset(str(case["id"]) for case in cases)

    def case(self, case_id):
        return next(dict(case) for case in self.cases if case["id"] == case_id)


def test_default_cad_test_runner_uses_repository_owned_scorer():
    from app.evaluation.cad_test_runner import CadTestRunnerConfig

    config = CadTestRunnerConfig(root=Path("D:/temporary/cad-test-output"))

    script = config.resolved_script()

    assert script.is_relative_to(Path(__file__).resolve().parents[1])
    assert script.name == "score_step.py"
    assert "CAD_test" not in str(script)


def test_dataset_loader_reads_data_without_importing_external_package(tmp_path):
    from benchmark.cad_test_engine.dataset import load_dataset

    dataset_root = tmp_path / "CAD_test"
    case_root = dataset_root / "data" / "cases" / "ctb-test"
    case_root.mkdir(parents=True)
    (case_root / "prompt.txt").write_text("Create a test solid.", encoding="utf-8")
    case_json = case_root / "case.json"
    case_json.write_text("{}", encoding="utf-8")
    case_hash = hashlib.sha256(case_json.read_bytes()).hexdigest()
    manifest = {
        "status": "validated",
        "cases": [
            {
                "id": "ctb-test",
                "difficulty": "basic",
                "case_json_sha256": case_hash,
                "file_hashes": {},
            }
        ],
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    manifest["dataset_hash"] = hashlib.sha256(encoded).hexdigest()
    (dataset_root / "data" / "dataset-v1.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    dataset = load_dataset(tmp_path, verify_files=False)

    assert dataset.case("ctb-test")["prompt"] == "Create a test solid."


def test_batch_case_selection_preserves_dataset_modeling_path():
    from benchmark.cad_test_batch import _select_cases

    dataset = _RouteDataset(
        [
            {
                "id": "X01",
                "prompt": "Create a laser-cut profile.",
                "difficulty": "moderate",
                "path": "2d",
            },
            {
                "id": "A01",
                "prompt": "Create an assembly.",
                "difficulty": "complex",
                "path": "assembly",
            },
        ]
    )

    selected = _select_cases(dataset, "all", None)

    assert [case["path"] for case in selected] == ["2d", "assembly"]


def test_default_runner_mounts_repository_engine_for_scoring(tmp_path, monkeypatch):
    from app.evaluation.cad_test_runner import CadTestRunnerConfig, run_cad_test

    dataset_root = tmp_path / "dataset"
    (dataset_root / "data" / "cases" / "ctb-test").mkdir(parents=True)
    artifact = tmp_path / "result.step"
    artifact.write_bytes(b"step")
    output = tmp_path / "output"
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        output_mount = next(
            item
            for item in command
            if item.startswith("type=bind,source=") and ",destination=/out" in item
        )
        output_path = Path(
            output_mount.split("source=", 1)[1].split(",destination=", 1)[0]
        )
        output_path.mkdir(parents=True, exist_ok=True)
        (output_path / "score.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "geometry_valid": True,
                    "requirements_passed": 1,
                        "requirements_total": 1,
                        "checks": [],
                        "scorer_error": False,
                        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    result = run_cad_test(
        artifact_path=artifact,
        case_id="ctb-test",
        workflow_id="workflow-1",
        candidate_revision_id="revision-1",
        config=CadTestRunnerConfig(
            root=output,
            dataset_root=dataset_root,
            scorer_image="cad-agent-local",
        ),
    )

    assert result["status"] == "pass"
    assert commands
    assert commands[0][0] == "podman"
    assert "/code/score_step.py" in commands[0]
    assert not any("CAD_test" in item for item in commands[0])


def test_live_benchmark_row_contains_process_evaluation_metrics():
    from benchmark.live import benchmark_row_from_live_report

    row = benchmark_row_from_live_report(
        case={"id": "ctb-00000007", "description": "create a cylinder", "path": "extrude_cut"},
        response=SimpleNamespace(
            success=True,
            inspect_report=SimpleNamespace(printable=True, verdict="pass"),
        ),
        live_report={
            "status": "finished",
            "tool_calls": [
                {"call_kind": "workflow_activity", "name": "agent_v2.plan", "status": "passed"},
                {"call_kind": "cad_operation", "name": "make_cylinder", "status": "passed"},
                {"call_kind": "workflow_activity", "name": "agent_v2.repair_source", "status": "passed", "attempt": 2},
            ],
            "timeline": [
                {"event_type": "candidate_sealed", "status": "passed"},
                {"event_type": "visual_validation_finished", "status": "passed"},
                {"event_type": "session_finished", "payload": {"business_status": "succeeded"}},
            ],
            "complexity_profile": {
                "operation": {
                    "workflow_activity_count": 2,
                    "cad_operation_count": 1,
                    "repair_count": 1,
                    "candidate_count": 1,
                }
            },
            "repair_gain": {"repair_count": 1, "status": "measured"},
            "resource_cost": {"status": "measured", "total_duration_ms": 1200},
            "quality_matrix": {
                "geometry": {"status": "passed"},
                "dfm": {"status": "passed"},
                "visual": {"status": "passed"},
                "lifecycle": {"status": "succeeded"},
            },
            "evidence": [
                {
                    "source_kind": "agent_v2.validate_geometry",
                    "observed": {
                        "artifacts": [
                            {"format": "step", "parseable": True, "valid": True, "solid_count": 1},
                            {"format": "stl", "parseable": True, "valid": True, "solid_count": 1},
                        ]
                    },
                }
            ],
        },
        report_path="reports/live/live-live-1.json",
    )

    assert row["process_evaluation"] == {
        "status": "measured",
        "workflow_activity_count": 2,
        "cad_operation_count": 1,
        "repair_count": 1,
        "candidate_count": 1,
        "retry_call_count": 1,
        "failed_call_count": 0,
        "total_call_count": 3,
        "sequence_signature": None,
        "structural_signature": None,
        "geometry_status": "passed",
        "dfm_status": "passed",
        "visual_status": "passed",
        "lifecycle_status": "succeeded",
        "total_duration_ms": 1200,
        "activity_duration_ms": None,
        "duration_status": "unavailable",
        "repair_gain_status": "measured",
        "improved_check_count": 0,
        "remaining_failure_count": 0,
    }


def test_live_benchmark_row_embeds_process_snapshot_for_batch_report():
    from benchmark.live import benchmark_row_from_live_report

    tool_calls = [
        {
            "call_kind": "workflow_activity",
            "name": "agent_v2.plan",
            "status": "passed",
            "duration_ms": 125,
        }
    ]
    timeline = [
        {
            "event_type": "activity_finished",
            "name": "agent_v2.plan",
            "duration_ms": 125,
        }
    ]
    resource_cost = {
        "status": "measured",
        "total_duration_ms": 125,
        "duration": {"status": "measured", "activity_duration_ms": 125},
    }
    evidence = [{"source_kind": "agent_v2.validate_geometry", "observed": {}}]

    row = benchmark_row_from_live_report(
        case={"id": "P01", "description": "create a cube", "path": "extrude_cut"},
        response=SimpleNamespace(success=True),
        live_report={
            "status": "finished",
            "finished_at": "2026-11-23T03:42:01Z",
            "metrics": {"total_call_count": 1, "total_duration_ms": 125},
            "tool_calls": tool_calls,
            "timeline": timeline,
            "resource_cost": resource_cost,
            "complexity_profile": {"operation": {"workflow_activity_count": 1}},
            "quality_matrix": {"geometry": {"status": "passed"}},
            "repair_gain": {"status": "not_applicable"},
            "consistency": {"sequence_signature": "sequence"},
            "evidence": evidence,
        },
        report_path="reports/live/live-live-1.json",
    )

    snapshot = row["live_evaluation"]
    assert snapshot["status"] == "finished"
    assert snapshot["finished_at"] == "2026-11-23T03:42:01Z"
    assert snapshot["tool_calls"] == tool_calls
    assert snapshot["timeline"] == timeline
    assert snapshot["resource_cost"] == resource_cost
    assert snapshot["complexity_profile"]["operation"]["workflow_activity_count"] == 1
    assert snapshot["quality_matrix"]["geometry"]["status"] == "passed"
    assert snapshot["repair_gain"] == {"status": "not_applicable"}
    assert snapshot["consistency"] == {"sequence_signature": "sequence"}
    assert snapshot["evidence"] == evidence
