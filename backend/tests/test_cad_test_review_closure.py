import hashlib
import json
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest

from app.evaluation.cad_test_observer import config_from_environment
from app.evaluation.cad_test_runner import CadTestRunnerConfig, run_cad_test_2d
from app.evaluation.integration import attach_cad_test_observation, default_report_root
from app.evaluation.session import EvaluationSession
from app.evaluation.store import FileEvaluationStore
from benchmark import cad_test_batch, cad_test_live


def _session(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    store = FileEvaluationStore(tmp_path)
    store.create(EvaluationSession.create(
        mode="observe", evaluation_run_id="live-workflow", source_kind="benchmark",
        source_id="case", source_metadata={"dataset_hash": "b" * 64},
        workflow_id="workflow",
    ))
    store.update_identity("live-workflow", candidate_revision_id="revision")
    return store


def _score():
    return {
        "status": "pass", "workflow_id": "workflow", "candidate_revision_id": "revision",
        "case_id": "case", "dataset_hash": "b" * 64, "artifact_sha256": "a" * 64,
        "scorer_artifact_sha256": "a" * 64, "delivery_verified": True,
        "score": {"passed": True, "geometry_valid": True,
                  "requirements_passed": 1, "requirements_total": 1},
    }


@pytest.mark.parametrize("sealed", [False, True])
def test_observation_cannot_record_benchmark_pass(tmp_path, monkeypatch, sealed):
    store = _session(tmp_path, monkeypatch)
    if sealed:
        store.record_cad_test_seal("live-workflow", candidate_revision_id="revision",
                                   artifacts={"step": {"sha256": "a" * 64, "size_bytes": 4}})
    attach_cad_test_observation(
        {"workflow_run_id": "workflow", "candidate_revision_id": "revision"}, _score(),
        root=tmp_path,
    )
    report = json.loads(store.path_for("live-workflow").read_bytes())
    assert report["cad_test"]["status"] == "blocked"
    assert report["cad_test"]["verdict"] is None
    assert report["timeline"][-1]["status"] == "blocked"


def test_finish_preserves_cad_test_projection(tmp_path, monkeypatch):
    store = _session(tmp_path, monkeypatch)
    path = store.path_for("live-workflow")
    store.attach_cad_test_result("live-workflow", _score(), expected_artifact_sha256="a" * 64)
    before = json.loads(path.read_bytes())
    store.finish("live-workflow")
    after = json.loads(path.read_bytes())
    assert after["cad_test"] == before["cad_test"]
    assert after["quality_matrix"]["cad_test"] == before["quality_matrix"]["cad_test"]
    assert after["quality_matrix"]["cad_test"]["status"] == "passed"


def _environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_TEST_DATASET_ROOT", str(tmp_path / "dataset"))
    monkeypatch.delenv("CAD_TEST_ROOT", raising=False)
    monkeypatch.setenv("CAD_EVALUATION_REPORT_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("CAD_TEST_OUTPUT_ROOT", str(tmp_path / "scoring"))


@pytest.mark.parametrize("status", ["passed", "failed", "blocked", "error", "unknown",
                                     "unavailable", "not_applicable", None, "malformed", "missing"])
def test_live_cli_exit_depends_on_explicit_pass(tmp_path, monkeypatch, status):
    _environment(tmp_path, monkeypatch)
    root = tmp_path / "reports"
    path = FileEvaluationStore(root).path_for("live-workflow")
    path.parent.mkdir(parents=True)
    if status != "missing":
        payload = {} if status is None else {"cad_test": {"status": status, "verdict": status == "passed"}}
        path.write_text("{" if status == "malformed" else json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(cad_test_live, "attach_cad_test_result", lambda *a, **kw: "live-workflow")
    code = cad_test_live.main([
        "--artifact", "result.step", "--case", "case", "--workflow-id", "workflow",
        "--candidate-revision-id", "revision", "--cad-test-root", str(tmp_path / "dataset"),
        "--report-root", str(root),
    ])
    assert code == (0 if status == "passed" else 1)


def test_worker_and_live_cli_share_config(tmp_path, monkeypatch):
    _environment(tmp_path, monkeypatch)
    root = tmp_path / "reports"
    assert default_report_root() == root
    config = config_from_environment()
    assert config.dataset_root == tmp_path / "dataset"
    assert config.root == tmp_path / "scoring"
    observed = []

    def attach(*args, **kwargs):
        observed.append((kwargs["root"], kwargs["config"]))
        return None

    monkeypatch.setattr(cad_test_live, "attach_cad_test_result", attach)
    assert cad_test_live.main([
        "--artifact", "result.step", "--case", "case", "--workflow-id", "workflow",
        "--candidate-revision-id", "revision",
    ]) == 2
    assert observed == [(root, config)]


@pytest.mark.parametrize("flag", ["--dataset-root", "--report-root"])
def test_batch_live_rejects_mismatched_worker_paths(tmp_path, monkeypatch, capsys, flag):
    _environment(tmp_path, monkeypatch)
    monkeypatch.setattr(cad_test_batch, "load_dataset", lambda _: pytest.fail("must validate paths before loading"))
    with pytest.raises(SystemExit) as raised:
        cad_test_batch.main(["live", flag, str(tmp_path / "wrong")])
    assert raised.value.code == 2
    assert "configured" in capsys.readouterr().err


def _dxf(path, variant):
    document = ezdxf.new(units=ezdxf.units.MM)
    model = document.modelspace()
    if variant == "single-line":
        model.add_line((0, 0), (15, 0))
    else:
        model.add_lwpolyline([(0, 0), (30, 0), (30, 20), (0, 20)], close=variant != "open")
        model.add_circle((11 if variant == "moved" else 10, 10), 3 if variant == "scaled" else 2)
        if variant == "extra":
            model.add_circle((20, 10), 2)
    document.saveas(path)


@pytest.mark.parametrize("variant", ["match", "single-line", "moved", "scaled", "extra", "open", "missing-truth"])
def test_dxf_scorer_uses_case_geometry(tmp_path, variant):
    case = tmp_path / "case"
    case.mkdir()
    artifact = tmp_path / "submitted.dxf"
    _dxf(artifact, variant)
    if variant != "missing-truth":
        _dxf(case / "reference.dxf", "match")
        (case / "checks.json").write_text(
            json.dumps({"schema_version": "cad-test-dxf-checks.v1", "tolerance_mm": 0.01}),
            encoding="utf-8",
        )
    score = tmp_path / "score.json"
    script = Path(__file__).resolve().parents[1] / "benchmark" / "cad_test_engine" / "score_dxf.py"
    subprocess.run([sys.executable, str(script), str(artifact), str(case), str(score)], check=False)
    result = json.loads(score.read_bytes())
    assert result["passed"] is (variant == "match")
    assert result["scorer_error"] is (variant == "missing-truth")
    assert result["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()


def _verified_dxf_dataset(root, *, bind_truth):
    case = root / "data" / "cases" / "case"
    case.mkdir(parents=True)
    (case / "case.json").write_text("{}", encoding="utf-8")
    (case / "checks.json").write_text(
        '{"schema_version":"cad-test-dxf-checks.v1","tolerance_mm":0.01}', encoding="utf-8",
    )
    _dxf(case / "reference.dxf", "match")
    controls = root / "reports" / "controls-validation.json"
    controls.parent.mkdir()
    controls.write_text("{}", encoding="utf-8")
    manifest = {
        "status": "validated",
        "controls_sha256": hashlib.sha256(controls.read_bytes()).hexdigest(),
        "cases": [{"id": "case", "path": "2d",
                   "case_json_sha256": hashlib.sha256((case / "case.json").read_bytes()).hexdigest(),
                   "file_hashes": {
                       entry.relative_to(root).as_posix(): hashlib.sha256(entry.read_bytes()).hexdigest()
                       for entry in (case / "reference.dxf", case / "checks.json") if bind_truth
                   }}],
    }
    manifest["dataset_hash"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    (root / "data" / "dataset-v1.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest["dataset_hash"]


@pytest.mark.parametrize("bind_truth", [True, False])
def test_dxf_runner_requires_manifest_bound_truth(tmp_path, monkeypatch, bind_truth):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    dataset_root = tmp_path / "dataset"
    dataset_hash = _verified_dxf_dataset(dataset_root, bind_truth=bind_truth)
    artifact = tmp_path / "submission.dxf"
    _dxf(artifact, "match")
    execute = subprocess.run
    invoked = []

    def local_scorer(command, **kwargs):
        invoked.append(command)
        mounts = [dict(option.split("=", 1) for option in command[index + 1].split(",") if "=" in option)
                  for index, token in enumerate(command) if token == "--mount"]
        paths = {mount["destination"]: Path(mount["source"]) for mount in mounts}
        return execute([
            sys.executable, str(paths["/code"] / "score_dxf.py"), str(artifact),
            str(paths["/case"]), str(paths["/out"] / "score.json"),
        ], **kwargs)

    monkeypatch.setattr(subprocess, "run", local_scorer)
    result = run_cad_test_2d(
        artifact_path=artifact, case_id="case", workflow_id="workflow", candidate_revision_id="revision",
        config=CadTestRunnerConfig(root=tmp_path / "results", dataset_root=dataset_root, scorer_image="test"),
    )
    if bind_truth:
        assert invoked
        assert result["status"] == "pass"
        assert result["dataset_hash"] == dataset_hash
        assert result["delivery_verified"] is True
    else:
        assert not invoked
        assert result["status"] == "evaluation_error"
        assert result["reason"] == "dxf_case_truth_unavailable"


@pytest.mark.docker
@pytest.mark.parametrize("variant, status", [("match", "passed"), ("moved", "failed")])
def test_live_cli_scores_sealed_dxf_in_container(tmp_path, monkeypatch, variant, status):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    _environment(tmp_path, monkeypatch)
    dataset_root = tmp_path / "dataset"
    dataset_hash = _verified_dxf_dataset(dataset_root, bind_truth=True)
    artifact = tmp_path / "submission.dxf"
    _dxf(artifact, variant)
    artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    store = FileEvaluationStore(tmp_path / "reports")
    store.create(EvaluationSession.create(
        mode="observe", evaluation_run_id="live-workflow", source_kind="benchmark",
        source_id="case", source_metadata={"dataset_hash": dataset_hash, "modeling_path": "2d"},
        workflow_id="workflow",
    ))
    store.update_identity("live-workflow", candidate_revision_id="revision")
    store.record_cad_test_seal("live-workflow", candidate_revision_id="revision",
                               artifacts={"dxf": {"sha256": artifact_hash, "size_bytes": artifact.stat().st_size}})
    code = cad_test_live.main([
        "--artifact", str(artifact), "--case", "case", "--workflow-id", "workflow",
        "--candidate-revision-id", "revision",
    ])
    report = json.loads(store.finish("live-workflow").read_bytes())
    result = report["cad_test"]
    assert result["status"] == status, result
    assert result["identity"]["artifact_match"] is True
    assert result["identity"]["scorer_artifact_sha256"] == artifact_hash
    assert result["identity"]["dataset_match"] is True
    assert report["quality_matrix"]["cad_test"]["status"] == status
    assert code == (0 if status == "passed" else 1)
