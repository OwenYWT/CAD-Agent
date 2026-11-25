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
_ARTIFACT_KINDS = {"step", "fcstd", "stl", "dxf"}


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
    for kind, candidates in grouped.items():
        if len(candidates) == 1:
            rows[kind] = candidates[0]
            continue
        primary = [item for item in candidates if item.get("is_primary") is True]
        if not primary and modeling_path == "assembly":
            primary = [
                item
                for item in candidates
                if Path(str(item.get("filename") or "")).name.startswith("combine-")
            ]
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
    result: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "case_id": case_id,
        "workflow_id": str(workflow_id),
        "candidate_revision_id": str(candidate_revision_id),
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
        elif source_kind != "user_task":
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
