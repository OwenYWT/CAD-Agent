"""Developer-only CADTest runner owned by CAD-Agent."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

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

    scorer_error = score.get("scorer_error") is True or completed.returncode != 0
    passed = score.get("passed") is True and not scorer_error
    return {
        "status": "evaluation_error" if scorer_error else ("pass" if passed else "fail"),
        "case_id": case_id,
        "workflow_id": workflow_id,
        "candidate_revision_id": candidate_revision_id,
        "artifact_sha256": artifact_sha256,
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
    (paths["step"].parent / "delivery-inputs.json").write_text(
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
    if completed.returncode != 0 and result.get("status") != "error":
        result["status"] = "failed"
    elif result.get("status") not in {"error", "failed"}:
        result["status"] = "passed" if result.get("passed") is True else "failed"
    return result


def run_cad_test_bundle(
    *,
    artifacts: Mapping[str, Path],
    case_id: str,
    workflow_id: str,
    candidate_revision_id: str,
    config: CadTestRunnerConfig,
) -> dict[str, Any] | None:
    """Combine independent STEP scoring and native delivery evidence."""

    step = artifacts.get("step")
    if step is None:
        return _error_result(
            artifact_sha256=None,
            workflow_id=workflow_id,
            candidate_revision_id=candidate_revision_id,
            case_id=case_id,
            reason="step_artifact_missing",
        ) if resolve_mode() is EvaluationMode.OBSERVE else None
    result = run_cad_test(
        artifact_path=Path(step),
        case_id=case_id,
        workflow_id=workflow_id,
        candidate_revision_id=candidate_revision_id,
        config=config,
    )
    if result is None:
        return None
    required = {"step", "fcstd", "stl"}
    if set(artifacts) != required or any(
        not Path(artifacts[key]).is_file() for key in required
    ):
        result["delivery_verified"] = None
        result["native_delivery"] = {
            "status": "unavailable",
            "reason": "required_delivery_artifacts_missing",
        }
        return result
    native = run_native_delivery_check(artifacts=artifacts, config=config)
    result["native_delivery"] = native
    result["delivery_verified"] = (
        native.get("passed") if isinstance(native.get("passed"), bool) else None
    )
    return result
