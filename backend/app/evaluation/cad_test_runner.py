"""Developer-only CADTest runner owned by CAD-Agent."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from benchmark.cad_test_engine.dataset import load_dataset

from .mode import EvaluationMode, resolve_mode


_ENGINE_ROOT = Path(__file__).resolve().parents[2] / "benchmark" / "cad_test_engine"
_DEFAULT_SCORER_IMAGE = "cad-agent-sandbox:latest"


@dataclass(frozen=True)
class CadTestRunnerConfig:
    root: Path
    scorer_script: Path | None = None
    scorer_image: str = ""
    dataset_root: Path | None = None
    timeout_seconds: int = 180
    python_executable: str = sys.executable

    def resolved_script(self) -> Path:
        return (self.scorer_script or _ENGINE_ROOT / "score_step.py").resolve()

    def resolved_delivery_script(self) -> Path:
        return (_ENGINE_ROOT / "check_native_delivery.py").resolve()

    def resolved_case(self, case_id: str) -> Path | None:
        if self.dataset_root is None:
            return None
        configured = Path(self.dataset_root).resolve()
        candidates = (
            configured / "data" / "cases" / case_id,
            configured / "CAD_test" / "data" / "cases" / case_id,
            configured / "cases" / case_id,
        )
        return next((candidate for candidate in candidates if candidate.is_dir()), None)

    def resolved_image(self) -> str:
        configured = self.scorer_image or os.environ.get("CAD_TEST_SCORER_IMAGE")
        if configured:
            return configured
        configured = os.environ.get("SANDBOX_IMAGE")
        if configured:
            return configured
        try:
            from app.config import settings

            return settings.sandbox_image
        except Exception:
            return _DEFAULT_SCORER_IMAGE

    def resolved_dataset_hash(self) -> str | None:
        if self.dataset_root is None:
            return None
        try:
            return load_dataset(Path(self.dataset_root), verify_files=True).dataset_hash
        except (OSError, ValueError, KeyError, TypeError):
            return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _podman_bind_mount(source: Path, destination: str, *, readonly: bool) -> list[str]:
    options = f"type=bind,source={source.resolve()},destination={destination}"
    if readonly:
        options += ",readonly"
    return ["--mount", options]


def _error_result(
    *,
    artifact_sha256: str | None,
    workflow_id: str,
    candidate_revision_id: str,
    case_id: str,
    reason: str,
    error: Exception | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "evaluation_error",
        "reason": reason,
        "case_id": case_id,
        "workflow_id": workflow_id,
        "candidate_revision_id": candidate_revision_id,
        "score": {"passed": False, "scorer_error": True},
    }
    if artifact_sha256 is not None:
        result["artifact_sha256"] = artifact_sha256
    if error is not None:
        result["error"] = f"{type(error).__name__}: {str(error)[:1000]}"
    return result


def _load_score(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("CADTest score must be a JSON object")
    return value


def _run_internal_scorer(
    *,
    artifact: Path,
    case: Path,
    output: Path,
    config: CadTestRunnerConfig,
) -> tuple[int, str, str]:
    script = config.resolved_script()
    if not script.is_file():
        raise FileNotFoundError(str(script))
    command = [
        "podman",
        "run",
        "--rm",
        "--network",
        "none",
        "--memory",
        "2g",
        "--cpus",
        "1",
        "--pids-limit",
        "256",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        *_podman_bind_mount(script.parent, "/code", readonly=True),
        *_podman_bind_mount(case, "/case", readonly=True),
        *_podman_bind_mount(artifact.parent, "/input", readonly=True),
        *_podman_bind_mount(output, "/out", readonly=False),
        "--entrypoint",
        "python",
        config.resolved_image(),
        "/code/score_step.py",
        f"/input/{artifact.name}",
        "/case",
        "/out/score.json",
    ]
    completed = subprocess.run(
        command,
        cwd=str(script.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=config.timeout_seconds,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def run_cad_test(
    *,
    artifact_path: Path,
    case_id: str,
    workflow_id: str,
    candidate_revision_id: str,
    config: CadTestRunnerConfig,
) -> dict[str, Any] | None:
    """Run CADTest against an existing artifact without executing the model again."""

    if resolve_mode() is not EvaluationMode.OBSERVE:
        return None

    artifact = Path(artifact_path).resolve()
    if not artifact.is_file():
        return _error_result(
            artifact_sha256=None,
            workflow_id=workflow_id,
            candidate_revision_id=candidate_revision_id,
            case_id=case_id,
            reason="artifact_not_found",
        )

    artifact_sha256 = _sha256(artifact)
    output = config.root / "cad_test" / f"run-{uuid4()}"
    output.mkdir(parents=True, exist_ok=True)
    score_path = output / "score.json"
    case = config.resolved_case(case_id)
    if config.scorer_script is None and case is None:
        return _error_result(
            artifact_sha256=artifact_sha256,
            workflow_id=workflow_id,
            candidate_revision_id=candidate_revision_id,
            case_id=case_id,
            reason="dataset_case_not_found",
        )
    try:
        if config.scorer_script is not None:
            command = [
                config.python_executable,
                str(config.scorer_script.resolve()),
                "score",
                "--case",
                case_id,
                "--step",
                str(artifact),
                "--out",
                str(output),
                "--image",
                config.resolved_image(),
            ]
            completed = subprocess.run(
                command,
                cwd=str(config.scorer_script.resolve().parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=config.timeout_seconds,
                check=False,
            )
        else:
            returncode, stdout, stderr = _run_internal_scorer(
                artifact=artifact,
                case=case,
                output=output,
                config=config,
            )
            completed = subprocess.CompletedProcess(
                args=[], returncode=returncode, stdout=stdout, stderr=stderr
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _error_result(
            artifact_sha256=artifact_sha256,
            workflow_id=workflow_id,
            candidate_revision_id=candidate_revision_id,
            case_id=case_id,
            reason="scorer_execution_failed",
            error=exc,
        )

    (output / "scorer.log").write_text(
        completed.stdout + "\n" + completed.stderr,
        encoding="utf-8",
    )
    if not score_path.is_file():
        return _error_result(
            artifact_sha256=artifact_sha256,
            workflow_id=workflow_id,
            candidate_revision_id=candidate_revision_id,
            case_id=case_id,
            reason="score_missing",
        )
    try:
        score = _load_score(score_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _error_result(
            artifact_sha256=artifact_sha256,
            workflow_id=workflow_id,
            candidate_revision_id=candidate_revision_id,
            case_id=case_id,
            reason="score_invalid",
            error=exc,
        )

    scorer_artifact_sha256 = score.get("artifact_sha256")
    artifact_identity_mismatch = (
        (config.scorer_script is None or scorer_artifact_sha256 is not None)
        and str(scorer_artifact_sha256) != artifact_sha256
    )
    scorer_error = (
        score.get("scorer_error") is True
        or completed.returncode != 0
        or artifact_identity_mismatch
    )
    passed = score.get("passed") is True and not scorer_error
    return {
        "status": "evaluation_error" if scorer_error else ("pass" if passed else "fail"),
        "case_id": case_id,
        "workflow_id": workflow_id,
        "candidate_revision_id": candidate_revision_id,
        "artifact_sha256": artifact_sha256,
        "scorer_artifact_sha256": (
            str(scorer_artifact_sha256) if scorer_artifact_sha256 is not None else None
        ),
        "dataset_hash": config.resolved_dataset_hash(),
        **(
            {"reason": "artifact_identity_mismatch"}
            if artifact_identity_mismatch
            else {}
        ),
        "scorer_image": config.resolved_image(),
        "scorer_script_sha256": _sha256(config.resolved_script()),
        "score": score,
    }


def run_native_delivery_check(
    *,
    artifacts: Mapping[str, Path],
    config: CadTestRunnerConfig,
) -> dict[str, Any]:
    """Run the repository-owned native-delivery verifier in Podman."""

    required = {"step", "fcstd", "stl"}
    if set(artifacts) != required or any(
        not Path(artifacts[key]).is_file() for key in required
    ):
        return {
            "status": "unavailable",
            "reason": "required_delivery_artifacts_missing",
        }
    paths = {key: Path(artifacts[key]).resolve() for key in required}
    if len({path.parent for path in paths.values()}) != 1:
        return {
            "status": "error",
            "reason": "delivery_artifacts_must_share_directory",
        }
    script = config.resolved_delivery_script()
    if not script.is_file():
        return {"status": "error", "reason": "scorer_not_found"}

    output = config.root / "cad_test" / f"delivery-{uuid4()}"
    output.mkdir(parents=True, exist_ok=True)
    (output / "delivery-inputs.json").write_text(
        json.dumps(
            {key: paths[key].name for key in ("step", "fcstd", "stl")},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    command = [
        "podman",
        "run",
        "--rm",
        "--network",
        "none",
        "--memory",
        "2g",
        "--cpus",
        "1",
        "--pids-limit",
        "256",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        *_podman_bind_mount(script.parent, "/code", readonly=True),
        *_podman_bind_mount(paths["step"].parent, "/input", readonly=True),
        *_podman_bind_mount(output, "/out", readonly=False),
        "--entrypoint",
        "/opt/freecad/bin/FreeCADCmd",
        config.resolved_image(),
        "-c",
        "import sys; sys.path.insert(0, '/code'); "
        "exec(compile(open('/code/check_native_delivery.py').read(), '/code/check_native_delivery.py', 'exec'))",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(script.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "error",
            "reason": "native_delivery_verifier_failed",
            "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
        }
    (output / "native-delivery-wrapper.log").write_text(
        completed.stdout + "\n" + completed.stderr,
        encoding="utf-8",
    )
    result_path = output / "native-delivery.json"
    if not result_path.is_file():
        return {"status": "error", "reason": "native_delivery_result_missing"}
    try:
        result = _load_score(result_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "error",
            "reason": "native_delivery_result_invalid",
            "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
        }
    if not isinstance(result.get("passed"), bool):
        return {"status": "error", "reason": "native_delivery_result_invalid"}
    if result.get("error") or result.get("status") == "error":
        result.update(status="error", passed=False)
    else:
        passed = (
            completed.returncode == 0 and result["passed"]
            and result.get("status") in (None, "passed")
        )
        result.update(status="passed" if passed else "failed", passed=passed)
    return result


def run_cad_test_2d(
    *,
    artifact_path: Path,
    case_id: str,
    workflow_id: str,
    candidate_revision_id: str,
    config: CadTestRunnerConfig,
) -> dict[str, Any] | None:
    """Score a DXF through the repository-owned 2D route."""

    if resolve_mode() is not EvaluationMode.OBSERVE:
        return None
    artifact = Path(artifact_path).resolve()
    if not artifact.is_file():
        return _error_result(
            artifact_sha256=None,
            workflow_id=workflow_id,
            candidate_revision_id=candidate_revision_id,
            case_id=case_id,
            reason="artifact_not_found",
        )
    artifact_sha256 = _sha256(artifact)
    dataset_hash = None
    output = config.root / "cad_test" / f"run-2d-{uuid4()}"
    output.mkdir(parents=True, exist_ok=True)
    score_path = output / "score.json"
    case = output / "case"
    if config.scorer_script is None:
        try:
            if config.dataset_root is None:
                raise ValueError("dataset root is required")
            dataset = load_dataset(config.dataset_root)
            dataset_hash = dataset.dataset_hash
            metadata = next((row for row in dataset.cases if row["id"] == case_id), {})
            if metadata.get("path") != "2d":
                raise ValueError("case must be registered for 2D scoring")
            case.mkdir()
            for filename in ("checks.json", "reference.dxf"):
                source = dataset.case_root(case_id) / filename
                expected = metadata["file_hashes"][source.relative_to(dataset.root).as_posix()]
                destination = case / filename
                shutil.copyfile(source, destination)
                if _sha256(destination) != expected:
                    raise ValueError("DXF truth changed while staging")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return _error_result(
                artifact_sha256=artifact_sha256,
                workflow_id=workflow_id,
                candidate_revision_id=candidate_revision_id,
                case_id=case_id,
                reason="dxf_case_truth_unavailable",
                error=exc,
            )
        score_path = output / "score" / "score.json"
        score_path.parent.mkdir()
    else:
        dataset_hash = config.resolved_dataset_hash()
    try:
        if config.scorer_script is not None:
            command = [
                config.python_executable,
                str(config.scorer_script.resolve()),
                "score",
                "--case",
                case_id,
                "--step",
                str(artifact),
                "--out",
                str(output),
                "--image",
                config.resolved_image(),
            ]
        else:
            command = [
                "podman",
                "run",
                "--rm",
                "--network",
                "none",
                *_podman_bind_mount(_ENGINE_ROOT, "/code", readonly=True),
                *_podman_bind_mount(case, "/case", readonly=True),
                *_podman_bind_mount(artifact.parent, "/input", readonly=True),
                *_podman_bind_mount(score_path.parent, "/out", readonly=False),
                "--entrypoint",
                "python",
                config.resolved_image(),
                "/code/score_dxf.py",
                f"/input/{artifact.name}",
                "/case",
                "/out/score.json",
            ]
        completed = subprocess.run(
            command,
            cwd=str(config.resolved_script().parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _error_result(
            artifact_sha256=artifact_sha256,
            workflow_id=workflow_id,
            candidate_revision_id=candidate_revision_id,
            case_id=case_id,
            reason="scorer_execution_failed",
            error=exc,
        )
    (output / "scorer.log").write_text(
        completed.stdout + "\n" + completed.stderr,
        encoding="utf-8",
    )
    if not score_path.is_file():
        return _error_result(
            artifact_sha256=artifact_sha256,
            workflow_id=workflow_id,
            candidate_revision_id=candidate_revision_id,
            case_id=case_id,
            reason="score_missing",
        )
    try:
        score = _load_score(score_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _error_result(
            artifact_sha256=artifact_sha256,
            workflow_id=workflow_id,
            candidate_revision_id=candidate_revision_id,
            case_id=case_id,
            reason="score_invalid",
            error=exc,
        )
    scorer_artifact_sha256 = score.get("artifact_sha256")
    artifact_identity_mismatch = (
        (config.scorer_script is None or scorer_artifact_sha256 is not None)
        and str(scorer_artifact_sha256) != artifact_sha256
    )
    scorer_error = (
        score.get("scorer_error") is True
        or completed.returncode != 0
        or artifact_identity_mismatch
    )
    return {
        "status": "evaluation_error" if scorer_error else ("pass" if score.get("passed") is True else "fail"),
        "case_id": case_id,
        "workflow_id": workflow_id,
        "candidate_revision_id": candidate_revision_id,
        "artifact_sha256": artifact_sha256,
        "scorer_artifact_sha256": (
            str(scorer_artifact_sha256) if scorer_artifact_sha256 is not None else None
        ),
        "dataset_hash": dataset_hash,
        "delivery_verified": not scorer_error and score.get("geometry_valid") is True,
        "score": score,
    }


def run_cad_test_bundle(
    *,
    artifacts: Mapping[str, Path],
    case_id: str,
    workflow_id: str,
    candidate_revision_id: str,
    config: CadTestRunnerConfig,
    modeling_path: str = "extrude_cut",
) -> dict[str, Any] | None:
    """Combine independent STEP scoring and native delivery evidence."""

    if modeling_path == "2d":
        dxf = artifacts.get("dxf")
        if dxf is None:
            return _error_result(
                artifact_sha256=None,
                workflow_id=workflow_id,
                candidate_revision_id=candidate_revision_id,
                case_id=case_id,
                reason="dxf_artifact_missing",
            ) if resolve_mode() is EvaluationMode.OBSERVE else None
        return run_cad_test_2d(
            artifact_path=Path(dxf),
            case_id=case_id,
            workflow_id=workflow_id,
            candidate_revision_id=candidate_revision_id,
            config=config,
        )
    if resolve_mode() is not EvaluationMode.OBSERVE:
        return None
    required = ("step", "fcstd", "stl")
    missing = [kind for kind in required if kind not in artifacts or not Path(artifacts[kind]).is_file()]
    if missing or set(artifacts) != set(required):
        return {
            **_error_result(
                artifact_sha256=None, workflow_id=workflow_id,
                candidate_revision_id=candidate_revision_id, case_id=case_id,
                reason="required_delivery_artifacts_missing" if missing else "unexpected_delivery_artifacts",
            ),
            "dataset_hash": config.resolved_dataset_hash(),
            "missing_artifacts": missing,
        }
    result = run_cad_test(
        artifact_path=Path(artifacts["step"]),
        case_id=case_id,
        workflow_id=workflow_id,
        candidate_revision_id=candidate_revision_id,
        config=config,
    )
    if result is None:
        return None
    if result["status"] not in {"pass", "passed", "fail"}:
        return result
    native = run_native_delivery_check(artifacts=artifacts, config=config)
    result["native_delivery"] = native
    result["delivery_verified"] = native.get("status") == "passed" and native.get("passed") is True
    if not result["delivery_verified"] and result["status"] != "fail":
        status = "fail" if native.get("status") == "failed" else "error"
        result.update(status=status, reason=native.get("reason") or "native_delivery_not_verified")
    return result
