"""Best-effort CADTest and native-delivery observation for developer runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from app.object_store import download_object
from benchmark.cad_test_engine.dataset import load_dataset, resolve_dataset_root

from .cad_test_runner import CadTestRunnerConfig, run_native_delivery_check
from .integration import attach_cad_test_artifacts, attach_cad_test_observation
from .mode import EvaluationMode, resolve_mode
from .store import FileEvaluationStore
from .integration import default_report_root, evaluation_run_id_for_workflow

logger = logging.getLogger(__name__)
_ARTIFACT_KINDS = {"step", "fcstd", "stl", "dxf"}
CAD_TEST_TERMINAL_STATUSES = frozenset({"passed", "failed", "error", "blocked", "not_applicable"})


def cad_test_required_from_environment() -> bool:
    return os.environ.get("CAD_TEST_REQUIRED", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }


def temporal_worker_build_id() -> str | None:
    value = os.environ.get("TEMPORAL_WORKER_BUILD_ID", "").strip()
    return value or None


def require_temporal_worker_build_id() -> str:
    build_id = temporal_worker_build_id()
    if build_id is None:
        raise RuntimeError("TEMPORAL_WORKER_BUILD_ID is required for CADTest evaluation")
    return build_id


async def dispatch_cad_test_observation(payload: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    if resolve_mode() is not EvaluationMode.OBSERVE or result.get("status") != "succeeded":
        return
    workflow_id = payload.get("workflow_run_id")
    revision_id = result.get("candidate_revision_id")
    if not workflow_id or not revision_id:
        return
    source = _report_source(payload, default_report_root()) or {}
    if source.get("kind") != "user_task" and not (
        source.get("kind") == "benchmark" and source.get("id") and source.get("dataset_hash")
    ):
        return

    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from app.config import settings
    from app.temporal_client import get_temporal_client
    from app.workflows.cad_test_observation import CadTestObservationWorkflow

    observation = {
        "payload": {"workflow_run_id": str(workflow_id)},
        "result": {
            "status": "succeeded",
            "candidate_revision_id": str(revision_id),
            "artifacts": result.get("artifacts"),
            "worker_build_id": temporal_worker_build_id(),
        },
    }
    try:
        async with asyncio.timeout(5):
            client = await get_temporal_client()
            await client.start_workflow(
                CadTestObservationWorkflow.run,
                observation,
                id=f"cad-test:{workflow_id}:{revision_id}",
                task_queue=settings.temporal_agent_v2_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                execution_timeout=timedelta(minutes=30),
            )
    except WorkflowAlreadyStartedError:
        return


def cad_test_observation_recorded(payload: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
    root = default_report_root()
    path = FileEvaluationStore(root).path_for(evaluation_run_id_for_workflow(payload["workflow_run_id"]))
    report = json.loads(path.read_text(encoding="utf-8"))
    score = report.get("cad_test")
    identity = score.get("identity") if isinstance(score, Mapping) else None
    return (
        isinstance(score, Mapping)
        and score.get("status") in CAD_TEST_TERMINAL_STATUSES
        and isinstance(identity, Mapping)
        and identity.get("candidate_revision_id") == str(result["candidate_revision_id"])
    )


def config_from_environment() -> CadTestRunnerConfig | None:
    dataset_root = os.environ.get("CAD_TEST_DATASET_ROOT") or os.environ.get("CAD_TEST_ROOT")
    if not dataset_root:
        return None
    output_root = os.environ.get("CAD_TEST_OUTPUT_ROOT")
    if output_root is None:
        output_root = str(default_report_root() / "cad_test")
    return CadTestRunnerConfig(
        root=Path(output_root).expanduser().resolve(),
        dataset_root=Path(dataset_root).expanduser().resolve(),
        scorer_image=os.environ.get("CAD_TEST_SCORER_IMAGE", ""),
    )


def validate_cad_test_worker_config(
    config: CadTestRunnerConfig | None = None,
    *,
    verify_runtime: bool = True,
) -> str:
    """Fail worker startup unless the complete scorer and native runtime is usable."""
    config = config or config_from_environment()
    if config is None or config.dataset_root is None:
        raise RuntimeError("CAD_TEST_DATASET_ROOT is required when CAD evaluation is enabled")
    try:
        dataset = load_dataset(config.dataset_root, verify_files=True)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("CADTest dataset is invalid or unavailable") from exc
    if not dataset.cases or not dataset.dataset_hash:
        raise RuntimeError("CADTest dataset has no verified cases")
    if not config.resolved_image():
        raise RuntimeError("CAD_TEST_SCORER_IMAGE or SANDBOX_IMAGE is required")
    if not config.resolved_script().is_file():
        raise RuntimeError("repository CADTest scorer is missing")
    if not config.resolved_delivery_script().is_file():
        raise RuntimeError("repository native-delivery verifier is missing")

    report_root = default_report_root()
    try:
        report_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=report_root):
            pass
    except OSError as exc:
        raise RuntimeError("CAD_EVALUATION_REPORT_ROOT is not writable by the Worker") from exc

    if verify_runtime:
        podman = shutil.which("podman")
        if podman is None:
            raise RuntimeError("Podman is required for CADTest and native delivery checks")
        image = config.resolved_image()
        try:
            present = subprocess.run(
                [podman, "image", "exists", image], capture_output=True, timeout=15, check=False,
            )
            if present.returncode != 0:
                raise RuntimeError(f"CADTest runtime image is unavailable: {image}")
            native = subprocess.run(
                [podman, "run", "--rm", "--network", "none", "--entrypoint",
                 "/opt/freecad/bin/FreeCADCmd", image, "--version"],
                capture_output=True, timeout=30, check=False,
            )
            if native.returncode != 0:
                raise RuntimeError("CADTest image lacks a working FreeCADCmd native-delivery runtime")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("CADTest container runtime preflight failed") from exc
    return dataset.dataset_hash


def resolve_live_cli_config(
    *, dataset_root: Path | None, report_root: Path | None,
) -> tuple[CadTestRunnerConfig, Path]:
    """Treat live CLI flags as assertions about shared deployment configuration."""
    config = config_from_environment()
    if config is None:
        raise ValueError("CAD_TEST_DATASET_ROOT must be configured for both the CLI and Worker")
    reports = default_report_root()
    if report_root is not None and report_root.expanduser().resolve() != reports:
        raise ValueError("--report-root differs from the configured CAD_EVALUATION_REPORT_ROOT")
    if dataset_root is not None:
        requested = dataset_root.expanduser().resolve()
        if requested != config.dataset_root:
            try:
                matches = resolve_dataset_root(requested) == resolve_dataset_root(config.dataset_root)
            except FileNotFoundError:
                matches = False
            if not matches:
                raise ValueError("--dataset-root differs from the configured CAD_TEST_DATASET_ROOT")
    return config, reports


def _report_source(payload: Mapping[str, Any], root: Path) -> Mapping[str, Any] | None:
    workflow_id = payload.get("workflow_run_id")
    if workflow_id is None:
        return None
    report_path = FileEvaluationStore(root).path_for(
        evaluation_run_id_for_workflow(workflow_id)
    )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    source = report.get("source")
    return source if isinstance(source, Mapping) else None


def _benchmark_source(payload: Mapping[str, Any], root: Path) -> Mapping[str, Any] | None:
    source = _report_source(payload, root)
    if source is None or source.get("kind") != "benchmark":
        return None
    return source


def _artifact_rows(
    result: Mapping[str, Any],
    *,
    modeling_path: str = "extrude_cut",
) -> dict[str, Mapping[str, Any]] | None:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return None
    for item in artifacts:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("artifact_kind") or "").lower()
        if kind not in _ARTIFACT_KINDS:
            continue
        grouped.setdefault(kind, []).append(item)
    rows: dict[str, Mapping[str, Any]] = {}
    assembly = modeling_path == "assembly" or any(
        "is_primary" in item for candidates in grouped.values() for item in candidates
    )
    for kind, candidates in grouped.items():
        if len(candidates) == 1 and not assembly:
            rows[kind] = candidates[0]
            continue
        primary = [item for item in candidates if item.get("is_primary") is True]
        if len(primary) != 1:
            return None
        rows[kind] = primary[0]
    return rows


def _record_benchmark_observer_failure(
    *,
    workflow_id: object,
    candidate_revision_id: object,
    case_id: str,
    reason: str,
    root: Path,
    artifact_kind: str | None = None,
    status: str = "blocked",
    error_type: str | None = None,
) -> None:
    source = _report_source(
        {"workflow_run_id": str(workflow_id)}, root
    ) or {}
    result: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "case_id": case_id,
        "workflow_id": str(workflow_id),
        "candidate_revision_id": str(candidate_revision_id),
        "expected_worker_build_id": source.get("expected_worker_build_id"),
        "worker_build_id": temporal_worker_build_id(),
    }
    if artifact_kind is not None:
        result["artifact_kind"] = artifact_kind
    if error_type is not None:
        result["error_type"] = error_type
    attach_cad_test_observation(
        {
            "workflow_run_id": str(workflow_id),
            "candidate_revision_id": str(candidate_revision_id),
        },
        result,
        root=root,
    )


async def observe_sealed_candidate(
    payload: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    root: Path | None = None,
    config: CadTestRunnerConfig | None = None,
) -> None:
    """Observe sealed benchmark scores or user-task native delivery in observe mode."""

    if resolve_mode() is not EvaluationMode.OBSERVE:
        return
    report_root = root or default_report_root()
    config = config or config_from_environment()
    source = _report_source(payload, report_root)
    source_kind = str(source.get("kind") or "") if source else ""
    benchmark_source = _benchmark_source(payload, report_root)
    case_id = str(benchmark_source.get("id") or "") if benchmark_source else None
    workflow_id = payload.get("workflow_run_id")
    candidate_revision_id = result.get("candidate_revision_id")
    if workflow_id is None or candidate_revision_id is None:
        return
    if case_id is None or str(result.get("status")) != "succeeded":
        if source_kind != "user_task" or str(result.get("status")) != "succeeded":
            return
    rows = _artifact_rows(
        result,
        modeling_path=str((benchmark_source or {}).get("modeling_path") or "extrude_cut"),
    )
    if not rows:
        if source_kind == "benchmark" and case_id:
            _record_benchmark_observer_failure(
                workflow_id=workflow_id,
                candidate_revision_id=candidate_revision_id,
                case_id=case_id,
                reason="artifact_selection_unavailable",
                root=report_root,
            )
        if source_kind != "user_task":
            return

    if source_kind == "user_task":
        native: dict[str, Any] = {
            "status": "unavailable",
            "reason": "cad_test_configuration_unavailable",
        }
        if config is not None and rows:
            with tempfile.TemporaryDirectory(prefix="cad_test_bundle_") as temporary:
                directory = Path(temporary)
                local_paths: dict[str, Path] = {}
                download_ok = True
                for kind, item in rows.items():
                    object_key = item.get("object_key")
                    expected_sha = str(item.get("sha256") or "")
                    expected_size = item.get("size_bytes")
                    if not object_key or len(expected_sha) != 64 or not isinstance(expected_size, int):
                        download_ok = False
                        break
                    destination = directory / f"result.{kind}"
                    try:
                        facts = await download_object(
                            str(object_key), destination, expected_size=expected_size
                        )
                    except Exception as exc:
                        logger.warning("CADTest artifact download failed: %s", type(exc).__name__)
                        download_ok = False
                        break
                    if (
                        facts.get("sha256") != expected_sha
                        or facts.get("size_bytes") != expected_size
                        or hashlib.sha256(destination.read_bytes()).hexdigest() != expected_sha
                    ):
                        logger.warning("CADTest artifact integrity mismatch for %s", kind)
                        download_ok = False
                        break
                    local_paths[kind] = destination
                if download_ok:
                    native = await asyncio.to_thread(
                        run_native_delivery_check,
                        artifacts=local_paths,
                        config=config,
                    )
        await asyncio.to_thread(
            attach_cad_test_observation,
            {
                "workflow_run_id": str(workflow_id),
                "candidate_revision_id": str(candidate_revision_id),
            },
            {
                "status": "not_applicable",
                "reason": "no_benchmark_case",
                "workflow_id": str(workflow_id),
                "candidate_revision_id": str(candidate_revision_id),
                "worker_build_id": temporal_worker_build_id(),
                "delivery_verified": native.get("passed")
                if isinstance(native.get("passed"), bool)
                else None,
                "native_delivery": native,
            },
            root=report_root,
        )
        return

    if case_id and rows:
        FileEvaluationStore(report_root).record_cad_test_seal(
            evaluation_run_id_for_workflow(workflow_id),
            candidate_revision_id=str(candidate_revision_id),
            artifacts={
                kind: {"sha256": item.get("sha256"), "size_bytes": item.get("size_bytes")}
                for kind, item in rows.items()
            },
        )

    if config is None:
        if source_kind == "benchmark" and case_id:
            _record_benchmark_observer_failure(
                workflow_id=workflow_id,
                candidate_revision_id=candidate_revision_id,
                case_id=case_id,
                reason="cad_test_configuration_unavailable",
                root=report_root,
            )
        return

    with tempfile.TemporaryDirectory(prefix="cad_test_bundle_") as temporary:
        directory = Path(temporary)
        local_paths: dict[str, Path] = {}
        for kind, item in rows.items():
            object_key = item.get("object_key")
            expected_sha = str(item.get("sha256") or "")
            expected_size = item.get("size_bytes")
            if not object_key or len(expected_sha) != 64 or not isinstance(expected_size, int):
                _record_benchmark_observer_failure(
                    workflow_id=workflow_id,
                    candidate_revision_id=candidate_revision_id,
                    case_id=case_id,
                    reason="artifact_metadata_missing",
                    root=report_root,
                    artifact_kind=kind,
                )
                return
            destination = directory / f"result.{kind}"
            try:
                facts = await download_object(
                    str(object_key), destination, expected_size=expected_size
                )
            except Exception as exc:
                logger.warning("CADTest artifact download failed: %s", type(exc).__name__)
                _record_benchmark_observer_failure(
                    workflow_id=workflow_id,
                    candidate_revision_id=candidate_revision_id,
                    case_id=case_id,
                    reason="artifact_download_failed",
                    root=report_root,
                    artifact_kind=kind,
                )
                return
            if (
                facts.get("sha256") != expected_sha
                or facts.get("size_bytes") != expected_size
                or hashlib.sha256(destination.read_bytes()).hexdigest() != expected_sha
            ):
                logger.warning("CADTest artifact integrity mismatch for %s", kind)
                _record_benchmark_observer_failure(
                    workflow_id=workflow_id,
                    candidate_revision_id=candidate_revision_id,
                    case_id=case_id,
                    reason="artifact_integrity_mismatch",
                    root=report_root,
                    artifact_kind=kind,
                )
                return
            local_paths[kind] = destination
        try:
            await asyncio.to_thread(
                attach_cad_test_artifacts,
                {
                    "workflow_run_id": str(workflow_id),
                    "candidate_revision_id": str(candidate_revision_id),
                },
                local_paths,
                case_id=case_id,
                modeling_path=str(benchmark_source.get("modeling_path") or "extrude_cut"),
                root=report_root,
                config=config,
            )
        except Exception as exc:
            logger.warning("CADTest sealed-candidate observation failed: %s", type(exc).__name__)
            try:
                _record_benchmark_observer_failure(
                    workflow_id=workflow_id,
                    candidate_revision_id=candidate_revision_id,
                    case_id=case_id,
                    reason="cad_test_attachment_failed",
                    root=report_root,
                    status="error",
                    error_type=type(exc).__name__,
                )
            except Exception as record_exc:
                logger.warning(
                    "CADTest attachment failure recording failed: %s",
                    type(record_exc).__name__,
                )
                raise
