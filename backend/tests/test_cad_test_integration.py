from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evaluation.cad_test_runner import CadTestRunnerConfig
from app.evaluation.integration import (
    attach_cad_test_artifacts,
    attach_cad_test_observation,
    attach_cad_test_result,
)
from app.evaluation.session import EvaluationSession
from app.evaluation.store import FileEvaluationStore


def _write_fake_cad_test(root: Path, artifact_sha256: str | None = None) -> None:
    package = root / "CAD_test"
    package.mkdir(parents=True)
    score = {
        "schema_version": "cad-eval-score.v1",
        "passed": True,
        "geometry_valid": True,
        "requirements_passed": 1,
        "requirements_total": 1,
        "checks": [],
        "scorer_error": False,
    }
    if artifact_sha256 is not None:
        score["artifact_sha256"] = artifact_sha256
    (package / "run.py").write_text(
        "import argparse, json\n"
        "from pathlib import Path\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('command')\n"
        "parser.add_argument('--case', required=True)\n"
        "parser.add_argument('--step', required=True)\n"
        "parser.add_argument('--out', required=True)\n"
        "parser.add_argument('--image', required=True)\n"
        "args = parser.parse_args()\n"
        f"Path(Path(__file__).parent.parent / 'score-value.json').write_text({json.dumps(json.dumps(score))}, encoding='utf-8')\n"
        "Path(args.out).mkdir(parents=True, exist_ok=True)\n"
        "Path(args.out, 'score.json').write_text(Path(Path(__file__).parent.parent / 'score-value.json').read_text(encoding='utf-8'), encoding='utf-8')\n",
        encoding="utf-8",
    )


def _config(root: Path) -> CadTestRunnerConfig:
    return CadTestRunnerConfig(
        root=root,
        scorer_script=root / "CAD_test" / "run.py",
        scorer_image="test-image",
    )


def test_observed_cad_test_is_attached_to_the_live_report(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    artifact = tmp_path / "result.step"
    artifact.write_bytes(b"step")
    _write_fake_cad_test(tmp_path)
    session = EvaluationSession.create(
        mode="observe",
        evaluation_run_id="live-workflow-1",
        source_kind="benchmark",
        source_id="case-1",
        workflow_id="workflow-1",
    )
    store = FileEvaluationStore(tmp_path / "reports")
    store.create(session)
    store.update_identity("live-workflow-1", candidate_revision_id="revision-1")

    run_id = attach_cad_test_result(
        {
            "workflow_run_id": "workflow-1",
            "candidate_revision_id": "revision-1",
        },
        artifact,
        case_id="case-1",
        root=tmp_path / "reports",
        config=_config(tmp_path),
    )

    assert run_id == "live-workflow-1"
    report = json.loads(
        store.path_for("live-workflow-1").read_text(encoding="utf-8")
    )
    assert report["cad_test"]["status"] == "unknown"
    assert report["cad_test"]["reason"] == "cad_test_evidence_incomplete"
    assert report["cad_test"]["identity"]["workflow_match"] is True
    assert report["cad_test"]["identity"]["candidate_revision_match"] is True
    assert report["status"] == "running"


def test_observed_cad_test_rejects_scorer_artifact_hash_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    artifact = tmp_path / "result.step"
    artifact.write_bytes(b"step")
    _write_fake_cad_test(tmp_path, artifact_sha256="f" * 64)
    session = EvaluationSession.create(
        mode="observe",
        evaluation_run_id="live-workflow-2",
        source_kind="benchmark",
        source_id="case-1",
        workflow_id="workflow-2",
    )
    store = FileEvaluationStore(tmp_path / "reports")
    store.create(session)
    store.update_identity("live-workflow-2", candidate_revision_id="revision-2")

    attach_cad_test_result(
        {
            "workflow_run_id": "workflow-2",
            "candidate_revision_id": "revision-2",
        },
        artifact,
        case_id="case-1",
        root=tmp_path / "reports",
        config=_config(tmp_path),
    )

    report = json.loads(
        store.path_for("live-workflow-2").read_text(encoding="utf-8")
    )
    assert report["cad_test"]["status"] == "error"
    assert report["cad_test"]["reason"] == "cad_test_scorer_error"
    assert report["cad_test"]["verdict"] is None


def test_observed_cad_test_bundle_can_produce_a_verified_report(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    _write_fake_cad_test(tmp_path)
    artifacts = {}
    for kind in ("step", "fcstd", "stl"):
        path = tmp_path / f"result.{kind}"
        path.write_bytes(kind.encode("ascii"))
        artifacts[kind] = path
    monkeypatch.setattr(
        "app.evaluation.cad_test_runner.run_native_delivery_check",
        lambda **kwargs: {
            "passed": True,
            "native_solids": 1,
            "step_solids": 1,
            "stl_facets": 12,
            "stl_bounds_match": True,
        },
    )
    session = EvaluationSession.create(
        mode="observe",
        evaluation_run_id="live-workflow-3",
        source_kind="benchmark",
        source_id="case-1",
        workflow_id="workflow-3",
    )
    store = FileEvaluationStore(tmp_path / "reports")
    store.create(session)
    store.update_identity("live-workflow-3", candidate_revision_id="revision-3")

    attach_cad_test_artifacts(
        {
            "workflow_run_id": "workflow-3",
            "candidate_revision_id": "revision-3",
        },
        artifacts,
        case_id="case-1",
        root=tmp_path / "reports",
        config=_config(tmp_path),
    )

    report = json.loads(
        store.path_for("live-workflow-3").read_text(encoding="utf-8")
    )
    assert report["cad_test"]["status"] == "passed"
    assert report["cad_test"]["delivery"]["status"] == "passed"
    assert any(
        event["event_type"] == "cad_test_finished"
        for event in report["timeline"]
    )
    assert report["status"] == "running"


def test_user_task_native_delivery_observation_is_not_a_dataset_pass(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    session = EvaluationSession.create(
        mode="observe",
        evaluation_run_id="live-workflow-4",
        source_kind="user_task",
        source_id="task-1",
        workflow_id="workflow-4",
    )
    store = FileEvaluationStore(tmp_path / "reports")
    store.create(session)
    store.update_identity("live-workflow-4", candidate_revision_id="revision-4")

    attach_cad_test_observation(
        {
            "workflow_run_id": "workflow-4",
            "candidate_revision_id": "revision-4",
        },
        {
            "status": "not_applicable",
            "reason": "no_benchmark_case",
            "workflow_id": "workflow-4",
            "candidate_revision_id": "revision-4",
            "delivery_verified": True,
            "native_delivery": {
                "status": "passed",
                "passed": True,
                "native_solids": 1,
                "step_solids": 1,
                "stl_facets": 12,
            },
        },
        root=tmp_path / "reports",
    )

    report = json.loads(
        store.path_for("live-workflow-4").read_text(encoding="utf-8")
    )
    assert report["cad_test"]["status"] == "not_applicable"
    assert report["cad_test"]["verdict"] is None
    assert report["cad_test"]["authority"] == "not_applicable"
    assert report["cad_test"]["delivery"]["status"] == "passed"
    assert report["status"] == "running"
    assert report["timeline"][-1]["status"] == "skipped"


def test_observed_cad_test_bundle_propagates_recording_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    session = EvaluationSession.create(
        mode="observe",
        evaluation_run_id="live-workflow-recording-failure",
        source_kind="benchmark",
        source_id="case-1",
        workflow_id="workflow-recording-failure",
    )
    store = FileEvaluationStore(tmp_path / "reports")
    store.create(session)
    store.update_identity(
        "live-workflow-recording-failure",
        candidate_revision_id="revision-recording-failure",
    )
    monkeypatch.setattr(
        "app.evaluation.integration.run_cad_test_bundle",
        lambda **kwargs: {
            "status": "pass",
            "case_id": "case-1",
            "workflow_id": "workflow-recording-failure",
            "candidate_revision_id": "revision-recording-failure",
            "score": {
                "passed": True,
                "geometry_valid": True,
                "requirements_passed": 1,
                "requirements_total": 1,
            },
            "delivery_verified": True,
            "native_delivery": {"passed": True},
        },
    )

    def fail_recording(*args, **kwargs):
        raise RuntimeError("report store unavailable")

    monkeypatch.setattr(FileEvaluationStore, "attach_cad_test_result", fail_recording)

    with pytest.raises(RuntimeError, match="report store unavailable"):
        attach_cad_test_artifacts(
            {
                "workflow_run_id": "workflow-recording-failure",
                "candidate_revision_id": "revision-recording-failure",
            },
            {},
            case_id="case-1",
            root=tmp_path / "reports",
            config=_config(tmp_path),
        )
def test_benchmark_result_rejects_case_id_that_differs_from_admitted_source(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    session = EvaluationSession.create(
        mode="observe",
        evaluation_run_id="live-workflow-case-boundary",
        source_kind="benchmark",
        source_id="ctb-00001411",
        workflow_id="workflow-case-boundary",
    )
    store = FileEvaluationStore(tmp_path / "reports")
    store.create(session)
    store.update_identity(
        "live-workflow-case-boundary",
        candidate_revision_id="revision-case-boundary",
    )
    called = []

    def run_bundle(**kwargs):
        called.append(kwargs)
        return {
            "status": "pass",
            "case_id": "ctb-00000007",
            "workflow_id": "workflow-case-boundary",
            "candidate_revision_id": "revision-case-boundary",
            "score": {
                "passed": True,
                "geometry_valid": True,
                "requirements_passed": 1,
                "requirements_total": 1,
            },
            "native_delivery": {"passed": True},
        }

    monkeypatch.setattr(
        "app.evaluation.integration.run_cad_test_bundle",
        run_bundle,
    )

    attach_cad_test_artifacts(
        {
            "workflow_run_id": "workflow-case-boundary",
            "candidate_revision_id": "revision-case-boundary",
        },
        {"step": tmp_path / "foreign.step"},
        case_id="ctb-00000007",
        root=tmp_path / "reports",
        config=_config(tmp_path),
    )

    report = json.loads(
        store.path_for("live-workflow-case-boundary").read_text(encoding="utf-8")
    )
    assert called == []
    assert report["cad_test"]["status"] == "blocked"
    assert report["cad_test"]["reason"] == "cad_test_case_identity_mismatch"
    assert report["cad_test"]["case"]["id"] == "ctb-00000007"


def test_benchmark_result_blocks_when_configured_dataset_fails_integrity(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    session = EvaluationSession.create(
        mode="observe",
        evaluation_run_id="live-workflow-dataset-boundary",
        source_kind="benchmark",
        source_id="case-1",
        source_metadata={"dataset_hash": "admitted-dataset"},
        workflow_id="workflow-dataset-boundary",
    )
    store = FileEvaluationStore(tmp_path / "reports")
    store.create(session)
    store.update_identity(
        "live-workflow-dataset-boundary",
        candidate_revision_id="revision-dataset-boundary",
    )
    called = []

    def run_test(**kwargs):
        called.append(kwargs)
        return {"status": "pass"}

    monkeypatch.setattr("app.evaluation.integration.run_cad_test", run_test)
    dataset_root = tmp_path / "dataset"
    (dataset_root / "data").mkdir(parents=True)
    (dataset_root / "data" / "dataset-v1.json").write_text(
        '{"status":"validated","dataset_hash":"admitted-dataset","cases":[]}',
        encoding="utf-8",
    )

    attach_cad_test_result(
        {
            "workflow_run_id": "workflow-dataset-boundary",
            "candidate_revision_id": "revision-dataset-boundary",
        },
        tmp_path / "result.step",
        case_id="case-1",
        root=tmp_path / "reports",
        config=CadTestRunnerConfig(
            root=tmp_path,
            dataset_root=dataset_root,
            scorer_script=tmp_path / "missing.py",
        ),
    )

    report = json.loads(
        store.path_for("live-workflow-dataset-boundary").read_text(encoding="utf-8")
    )
    assert called == []
    assert report["cad_test"]["status"] == "blocked"
    assert report["cad_test"]["reason"] == "cad_test_dataset_integrity_unavailable"
