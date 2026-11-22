"""Best-effort CADTest and native-delivery observation for developer runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from app.object_store import download_object

from .cad_test_runner import CadTestRunnerConfig, run_native_delivery_check
from .integration import attach_cad_test_artifacts, attach_cad_test_observation
from .mode import EvaluationMode, resolve_mode
from .store import FileEvaluationStore
from .integration import default_report_root, evaluation_run_id_for_workflow

logger = logging.getLogger(__name__)
_ARTIFACT_KINDS = {"step", "fcstd", "stl"}


def config_from_environment() -> CadTestRunnerConfig | None:
    dataset_root = os.environ.get("CAD_TEST_DATASET_ROOT") or os.environ.get("CAD_TEST_ROOT")
    if not dataset_root:
        return None
    output_root = os.environ.get("CAD_TEST_OUTPUT_ROOT")
    if output_root is None:
        output_root = str(default_report_root() / "cad_test")
    return CadTestRunnerConfig(
        root=Path(output_root),
        dataset_root=Path(dataset_root),
        scorer_image=os.environ.get("CAD_TEST_SCORER_IMAGE", ""),
    )


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


def _benchmark_case_id(payload: Mapping[str, Any], root: Path) -> str | None:
    source = _report_source(payload, root)
    if source is None or source.get("kind") != "benchmark":
        return None
    source_id = source.get("id")
    return str(source_id) if source_id else None


def _artifact_rows(result: Mapping[str, Any]) -> dict[str, Mapping[str, Any]] | None:
    rows: dict[str, Mapping[str, Any]] = {}
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        return None
    for item in artifacts:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("artifact_kind") or "").lower()
        if kind not in _ARTIFACT_KINDS:
            continue
        if kind in rows:
            return None
        rows[kind] = item
    return rows


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
    case_id = _benchmark_case_id(payload, report_root)
    if case_id is None or str(result.get("status")) != "succeeded":
        if source_kind != "user_task" or str(result.get("status")) != "succeeded":
            return
    rows = _artifact_rows(result)
    if not rows:
        if source_kind != "user_task":
            return

    workflow_id = payload.get("workflow_run_id")
    candidate_revision_id = result.get("candidate_revision_id")
    if workflow_id is None or candidate_revision_id is None:
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
                "delivery_verified": native.get("passed")
                if isinstance(native.get("passed"), bool)
                else None,
                "native_delivery": native,
            },
            root=report_root,
        )
        return

    if config is None:
        return

    with tempfile.TemporaryDirectory(prefix="cad_test_bundle_") as temporary:
        directory = Path(temporary)
        local_paths: dict[str, Path] = {}
        for kind, item in rows.items():
            object_key = item.get("object_key")
            expected_sha = str(item.get("sha256") or "")
            expected_size = item.get("size_bytes")
            if not object_key or len(expected_sha) != 64 or not isinstance(expected_size, int):
                return
            destination = directory / f"result.{kind}"
            try:
                facts = await download_object(
                    str(object_key), destination, expected_size=expected_size
                )
            except Exception as exc:
                logger.warning("CADTest artifact download failed: %s", type(exc).__name__)
                return
            if (
                facts.get("sha256") != expected_sha
                or facts.get("size_bytes") != expected_size
                or hashlib.sha256(destination.read_bytes()).hexdigest() != expected_sha
            ):
                logger.warning("CADTest artifact integrity mismatch for %s", kind)
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
                root=report_root,
                config=config,
            )
        except Exception as exc:
            logger.warning("CADTest sealed-candidate observation failed: %s", type(exc).__name__)
