from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from app.evaluation.cad_test_runner import (
    CadTestRunnerConfig,
    run_cad_test,
    run_cad_test_bundle,
    run_native_delivery_check,
)


def _write_fake_cad_test(root: Path, *, score: dict) -> None:
    package = root / "CAD_test"
    package.mkdir(parents=True)
    script = package / "run.py"
    script.write_text(
        """
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('command')
parser.add_argument('--case', required=True)
parser.add_argument('--step', type=Path, required=True)
parser.add_argument('--out', type=Path, required=True)
parser.add_argument('--image', required=True)
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
args.out.joinpath('score.json').write_text(SCORE, encoding='utf-8')
""".replace("SCORE", repr(json.dumps(score))),
        encoding="utf-8",
    )


def _config(root: Path) -> CadTestRunnerConfig:
    return CadTestRunnerConfig(
        root=root,
        scorer_script=root / "CAD_test" / "run.py",
        scorer_image="test-image",
        timeout_seconds=5,
    )


def test_disabled_mode_does_not_invoke_external_scorer(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "disabled")
    artifact = tmp_path / "result.step"
    artifact.write_bytes(b"step")

    result = run_cad_test(
        artifact_path=artifact,
        case_id="case-1",
        workflow_id="workflow-1",
        candidate_revision_id="revision-1",
        config=_config(tmp_path),
    )

    assert result is None
    assert not (tmp_path / "cad_test").exists()


def test_observe_mode_runs_external_scorer_and_binds_real_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    _write_fake_cad_test(
        tmp_path,
        score={
            "schema_version": "cad-eval-score.v1",
            "passed": True,
            "geometry_valid": True,
            "requirements_passed": 1,
            "requirements_total": 1,
            "checks": [],
            "scorer_error": False,
        },
    )
    artifact = tmp_path / "result.step"
    artifact.write_bytes(b"real-step-artifact")

    result = run_cad_test(
        artifact_path=artifact,
        case_id="case-1",
        workflow_id="workflow-1",
        candidate_revision_id="revision-1",
        config=_config(tmp_path),
    )

    assert result is not None
    assert result["status"] == "pass"
    assert result["workflow_id"] == "workflow-1"
    assert result["candidate_revision_id"] == "revision-1"
    assert result["artifact_sha256"]
    assert result["score"]["passed"] is True
    assert result["scorer_image"] == "test-image"


def test_scorer_failure_is_reported_without_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    _write_fake_cad_test(tmp_path, score={"scorer_error": True, "passed": False})
    artifact = tmp_path / "result.step"
    artifact.write_bytes(b"real-step-artifact")
    config = _config(tmp_path)
    config = CadTestRunnerConfig(
        root=config.root,
        scorer_script=tmp_path / "CAD_test" / "missing.py",
        scorer_image=config.scorer_image,
        timeout_seconds=config.timeout_seconds,
    )

    result = run_cad_test(
        artifact_path=artifact,
        case_id="case-1",
        workflow_id="workflow-1",
        candidate_revision_id="revision-1",
        config=config,
    )

    assert result is not None
    assert result["status"] == "evaluation_error"
    assert result["score"]["scorer_error"] is True
    assert result["artifact_sha256"]


def test_missing_artifact_is_evaluation_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    result = run_cad_test(
        artifact_path=tmp_path / "missing.step",
        case_id="case-1",
        workflow_id="workflow-1",
        candidate_revision_id="revision-1",
        config=_config(tmp_path),
    )

    assert result is not None
    assert result["status"] == "evaluation_error"
    assert result["reason"] == "artifact_not_found"


def test_bundle_rejects_step_only_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    _write_fake_cad_test(
        tmp_path,
        score={
            "schema_version": "cad-eval-score.v1",
            "passed": True,
            "geometry_valid": True,
            "requirements_passed": 1,
            "requirements_total": 1,
            "checks": [],
            "scorer_error": False,
        },
    )
    artifact = tmp_path / "result.step"
    artifact.write_bytes(b"real-step-artifact")

    result = run_cad_test_bundle(
        artifacts={"step": artifact},
        case_id="case-1",
        workflow_id="workflow-1",
        candidate_revision_id="revision-1",
        config=_config(tmp_path),
    )

    assert result is not None
    assert result["status"] == "evaluation_error"
    assert result["reason"] == "required_delivery_artifacts_missing"
    assert result["missing_artifacts"] == ["fcstd", "stl"]


def test_bundle_uses_dxf_route_without_native_3d_requirements(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    _write_fake_cad_test(
        tmp_path,
        score={
            "schema_version": "cad-eval-score.v1",
            "passed": True,
            "geometry_valid": True,
            "requirements_passed": 1,
            "requirements_total": 1,
            "checks": [],
            "scorer_error": False,
        },
    )

    artifact = tmp_path / "result.dxf"
    artifact.write_bytes(b"dxf-artifact")

    result = run_cad_test_bundle(
        artifacts={"dxf": artifact},
        case_id="case-1",
        workflow_id="workflow-1",
        candidate_revision_id="revision-1",
        config=_config(tmp_path),
        modeling_path="2d",
    )

    assert result["status"] == "pass"
    assert result["score"]["passed"] is True
    assert "native_delivery" not in result


def _write_verified_dataset(root: Path) -> Path:
    dataset_root = root / "dataset"
    case_root = dataset_root / "data" / "cases" / "case-1"
    case_root.mkdir(parents=True)
    (case_root / "prompt.txt").write_text("Create a test solid.", encoding="utf-8")
    case_json = case_root / "case.json"
    case_json.write_text("{}", encoding="utf-8")
    controls = dataset_root / "reports" / "controls-validation.json"
    controls.parent.mkdir(parents=True)
    controls.write_text("{}", encoding="utf-8")
    case_hash = hashlib.sha256(case_json.read_bytes()).hexdigest()
    controls_hash = hashlib.sha256(controls.read_bytes()).hexdigest()
    manifest = {
        "status": "validated",
        "controls_sha256": controls_hash,
        "cases": [
            {
                "id": "case-1",
                "difficulty": "basic",
                "case_json_sha256": case_hash,
                "file_hashes": {},
            }
        ],
    }
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    manifest["dataset_hash"] = hashlib.sha256(encoded).hexdigest()
    (dataset_root / "data" / "dataset-v1.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return dataset_root


def test_resolved_dataset_hash_requires_verified_dataset_files(tmp_path):
    dataset_root = _write_verified_dataset(tmp_path)
    config = CadTestRunnerConfig(root=tmp_path, dataset_root=dataset_root)

    assert config.resolved_dataset_hash()

    (dataset_root / "reports" / "controls-validation.json").write_text(
        '{"tampered": true}', encoding="utf-8"
    )

    assert config.resolved_dataset_hash() is None


def test_bundle_combines_verified_native_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    _write_fake_cad_test(
        tmp_path,
        score={
            "schema_version": "cad-eval-score.v1",
            "passed": True,
            "geometry_valid": True,
            "requirements_passed": 1,
            "requirements_total": 1,
            "checks": [],
            "scorer_error": False,
        },
    )
    artifacts = {}
    for kind in ("step", "fcstd", "stl"):
        path = tmp_path / f"result.{kind}"
        path.write_bytes(kind.encode("ascii"))
        artifacts[kind] = path
    monkeypatch.setattr(
        "app.evaluation.cad_test_runner.run_native_delivery_check",
        lambda **kwargs: {"status": "passed", "passed": True, "native_solids": 1, "step_solids": 1},
    )

    result = run_cad_test_bundle(
        artifacts=artifacts,
        case_id="case-1",
        workflow_id="workflow-1",
        candidate_revision_id="revision-1",
        config=_config(tmp_path),
    )

    assert result is not None
    assert result["delivery_verified"] is True
    assert result["native_delivery"]["native_solids"] == 1


def test_native_delivery_check_reports_missing_bundle_without_running_process(tmp_path):
    result = run_native_delivery_check(
        artifacts={"step": tmp_path / "result.step"},
        config=_config(tmp_path),
    )

    assert result == {
        "status": "unavailable",
        "reason": "required_delivery_artifacts_missing",
    }


def test_native_delivery_check_rejects_artifacts_from_different_directories(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    artifacts = {"step": first / "result.step", "fcstd": first / "result.fcstd", "stl": second / "result.stl"}
    for path in artifacts.values():
        path.write_bytes(b"artifact")

    result = run_native_delivery_check(artifacts=artifacts, config=_config(tmp_path))

    assert result == {
        "status": "error",
        "reason": "delivery_artifacts_must_share_directory",
    }


def test_native_delivery_nonzero_exit_cannot_report_passed(tmp_path, monkeypatch):
    artifacts = {}
    for kind in ("step", "fcstd", "stl"):
        path = tmp_path / f"result.{kind}"
        path.write_bytes(kind.encode("ascii"))
        artifacts[kind] = path
    input_sidecar = tmp_path / "delivery-inputs.json"
    input_sidecar.write_text("keep me", encoding="utf-8")

    def failed_wrapper(command, **kwargs):
        output_mount = next(
            item
            for item in command
            if item.startswith("type=bind,source=")
            and ",destination=/out" in item
        )
        output_path = Path(
            output_mount.split("source=", 1)[1].split(",destination=", 1)[0]
        )
        output_path.mkdir(parents=True, exist_ok=True)
        assert (output_path / "delivery-inputs.json").is_file()
        (output_path / "native-delivery.json").write_text(
            json.dumps({"passed": True}), encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 1, "", "verification failed")

    monkeypatch.setattr(subprocess, "run", failed_wrapper)

    result = run_native_delivery_check(
        artifacts=artifacts,
        config=_config(tmp_path),
    )

    assert result["status"] == "failed"
    assert result["passed"] is False
    assert input_sidecar.read_text(encoding="utf-8") == "keep me"


def test_internal_scorer_uses_mount_syntax_for_windows_host_paths(tmp_path, monkeypatch):
    from app.evaluation.cad_test_runner import _run_internal_scorer

    artifact = tmp_path / "result.step"
    case = tmp_path / "case"
    output = tmp_path / "output"
    artifact.write_bytes(b"step")
    case.mkdir()
    output.mkdir()
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    _run_internal_scorer(
        artifact=artifact,
        case=case,
        output=output,
        config=CadTestRunnerConfig(root=tmp_path, scorer_image="cad-agent-local"),
    )

    command = commands[0]
    mounts = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--mount"]
    assert mounts
    assert all(mount.startswith("type=bind,source=") for mount in mounts)
    assert any(f"source={case}" in mount and "destination=/case" in mount for mount in mounts)
