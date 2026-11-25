"""Benchmark adapter for the production durable execution path.

This module deliberately does not create a project, branch, revision, model,
or validation result. A live benchmark must be pointed at an existing workspace
and must consume the report written by the real API/Worker execution path.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from uuid import UUID

from app.evaluation.integration import evaluation_run_id_for_workflow
from app.evaluation.store import FileEvaluationStore
from app.models.schemas import GenerateResponse
from app.services.durable_submission import (
    DurableSubmission,
    submit_durable_workflow,
    wait_for_compatibility_response,
)
from app.workflows.temporal import confirm_mcad_workflow
from benchmark import metrics as M


@dataclass(frozen=True)
class LiveBenchmarkContext:
    principal: Any
    project_id: UUID
    branch_id: UUID
    expected_base_revision_id: UUID
    report_root: Path
    timeout_seconds: float = 180.0
    run_id: str = "live-benchmark"
    dataset_hash: str | None = None


Submitter = Callable[..., Awaitable[DurableSubmission]]
Waiter = Callable[..., Awaitable[GenerateResponse]]
Confirmer = Callable[..., Awaitable[None]]


def _run_id_from_report(report: Mapping[str, Any], report_path: Path) -> str:
    value = report.get("evaluation_run_id")
    if value:
        return str(value)
    stem = report_path.stem
    return stem.removeprefix("live_")


def _live_geometry_artifacts(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    artifacts: list[Mapping[str, Any]] = []
    for evidence in report.get("evidence", ()) or ():
        if not isinstance(evidence, Mapping):
            continue
        if evidence.get("source_kind") != "agent_v2.validate_geometry":
            continue
        observed = evidence.get("observed")
        if not isinstance(observed, Mapping):
            continue
        for artifact in observed.get("artifacts", ()) or ():
            if isinstance(artifact, Mapping):
                artifacts.append(artifact)
    return artifacts


def _has_nonempty_live_geometry(artifacts: list[Mapping[str, Any]]) -> bool:
    for artifact in artifacts:
        if artifact.get("valid") is not True:
            continue
        solid_count = artifact.get("solid_count")
        if isinstance(solid_count, bool):
            continue
        if isinstance(solid_count, (int, float)) and solid_count > 0:
            return True
    return False


def _live_visual_rendered(report: Mapping[str, Any]) -> bool:
    for event in report.get("timeline", ()) or ():
        if not isinstance(event, Mapping):
            continue
        if event.get("event_type") != "visual_validation_finished":
            continue
        payload = event.get("payload")
        if isinstance(payload, Mapping) and payload.get("status") == "rendered":
            return True
    return False


def _live_cad_test_gate(report: Mapping[str, Any]) -> dict[str, Any]:
    cad_test = report.get("cad_test")
    if not isinstance(cad_test, Mapping) or not cad_test:
        return {}
    delivery = cad_test.get("delivery")
    delivery = delivery if isinstance(delivery, Mapping) else {}
    gate: dict[str, Any] = {
        "cad_test_status": str(cad_test.get("status") or "unknown"),
        "native_delivery_verified": (
            True
            if delivery.get("status") == "passed"
            else False
            if delivery.get("status") == "failed"
            else None
        ),
    }
    for key in (
        "symmetric_difference_volume_mm3",
        "native_solids",
        "step_solids",
        "stl_facets",
        "stl_bounds_match",
    ):
        if key in delivery:
            gate[key] = delivery[key]
    return gate


def _process_evaluation(report: Mapping[str, Any]) -> dict[str, Any]:
    """Project live process evidence into one benchmark-row metric block."""

    calls = report.get("tool_calls")
    calls = calls if isinstance(calls, list) else []
    failed_call_count = sum(
        1
        for call in calls
        if isinstance(call, Mapping)
        and str(call.get("status") or "").lower() in {"failed", "fail", "error"}
    )
    retry_call_count = sum(
        1
        for call in calls
        if isinstance(call, Mapping)
        and (
            bool(call.get("replayed"))
            or (
                isinstance(call.get("attempt"), (int, float))
                and not isinstance(call.get("attempt"), bool)
                and int(call.get("attempt")) > 1
            )
        )
    )
    profile = report.get("complexity_profile")
    operation = profile.get("operation") if isinstance(profile, Mapping) else None
    operation = operation if isinstance(operation, Mapping) else {}
    quality = report.get("quality_matrix")
    quality = quality if isinstance(quality, Mapping) else {}
    resource = report.get("resource_cost")
    resource = resource if isinstance(resource, Mapping) else {}
    duration = resource.get("duration")
    duration = duration if isinstance(duration, Mapping) else {}
    consistency = report.get("consistency")
    consistency = consistency if isinstance(consistency, Mapping) else {}
    repair_gain = report.get("repair_gain")
    repair_gain = repair_gain if isinstance(repair_gain, Mapping) else {}

    measured = bool(calls or operation or quality or resource)
    return {
        "status": "measured" if measured else "unavailable",
        "workflow_activity_count": operation.get("workflow_activity_count", 0),
        "cad_operation_count": operation.get("cad_operation_count", 0),
        "repair_count": operation.get("repair_count", 0),
        "candidate_count": operation.get("candidate_count", 0),
        "retry_call_count": retry_call_count,
        "failed_call_count": failed_call_count,
        "total_call_count": consistency.get("total_call_count", len(calls)),
        "sequence_signature": consistency.get("sequence_signature"),
        "structural_signature": consistency.get("structural_signature"),
        "geometry_status": (
            quality.get("geometry", {}).get("status")
            if isinstance(quality.get("geometry"), Mapping)
            else "unavailable"
        ),
        "dfm_status": (
            quality.get("dfm", {}).get("status")
            if isinstance(quality.get("dfm"), Mapping)
            else "unavailable"
        ),
        "visual_status": (
            quality.get("visual", {}).get("status")
            if isinstance(quality.get("visual"), Mapping)
            else "unavailable"
        ),
        "lifecycle_status": (
            quality.get("lifecycle", {}).get("status")
            if isinstance(quality.get("lifecycle"), Mapping)
            else "unavailable"
        ),
        "total_duration_ms": resource.get(
            "total_duration_ms", report.get("metrics", {}).get("total_duration_ms", 0)
            if isinstance(report.get("metrics"), Mapping)
            else 0
        ),
        "activity_duration_ms": duration.get("activity_duration_ms"),
        "duration_status": duration.get("status", "unavailable"),
        "repair_gain_status": repair_gain.get("status", "not_applicable"),
        "improved_check_count": len(repair_gain.get("improved_checks") or [])
        if isinstance(repair_gain.get("improved_checks"), list)
        else 0,
        "remaining_failure_count": len(repair_gain.get("remaining_failures") or [])
        if isinstance(repair_gain.get("remaining_failures"), list)
        else 0,
    }


_LIVE_EVALUATION_SNAPSHOT_FIELDS = (
    "status",
    "finished_at",
    "metrics",
    "tool_calls",
    "timeline",
    "evidence",
    "consistency",
    "resource_cost",
    "complexity_profile",
    "quality_matrix",
    "repair_gain",
)


def _live_evaluation_snapshot(
    report: Mapping[str, Any], *, status: str
) -> dict[str, Any]:
    """Embed developer-facing live evidence without deriving new measurements."""

    snapshot = {
        key: copy.deepcopy(report[key])
        for key in _LIVE_EVALUATION_SNAPSHOT_FIELDS
        if key in report
    }
    snapshot.setdefault("status", status)
    return snapshot


def _artifact_gate_from_live_report(
    report: Mapping[str, Any], case: Mapping[str, Any]
) -> dict[str, Any]:
    artifacts = _live_geometry_artifacts(report)
    formats = {
        str(artifact.get("format", "")).casefold(): artifact
        for artifact in artifacts
    }
    cad_test_required = case.get("cad_test_required", True) is True
    if case.get("path") == "2d":
        dxf_readable = formats.get("dxf", {}).get("parseable") is True
        gate = {
            "source": "live_report",
            "dxf_readable": dxf_readable,
            "passed": dxf_readable,
        }
        cad_test_gate = _live_cad_test_gate(report)
        gate.update(cad_test_gate)
        if cad_test_gate and cad_test_required:
            gate["passed"] = gate["passed"] and (
                cad_test_gate.get("cad_test_status") == "passed"
            )
        return gate

    step_readable = formats.get("step", {}).get("parseable") is True
    stl_readable = formats.get("stl", {}).get("parseable") is True
    geometry_nonempty = _has_nonempty_live_geometry(artifacts)
    visual_rendered = _live_visual_rendered(report)
    gate = {
        "source": "live_report",
        "step_readable": step_readable,
        "stl_readable": stl_readable,
        "geometry_nonempty": geometry_nonempty,
        "visual_rendered": visual_rendered,
        "passed": (
            step_readable
            and stl_readable
            and geometry_nonempty
            and visual_rendered
        ),
    }
    cad_test_gate = _live_cad_test_gate(report)
    gate.update(cad_test_gate)
    if cad_test_gate and cad_test_required:
        gate["passed"] = bool(gate["passed"])
        gate["passed"] = gate["passed"] and (
            cad_test_gate.get("cad_test_status") == "passed"
            and cad_test_gate.get("native_delivery_verified") is True
        )
    return gate


def benchmark_row_from_live_report(
    *,
    case: Mapping[str, Any],
    response: GenerateResponse | Any,
    live_report: Mapping[str, Any] | None,
    report_path: Path | str,
    wall_time_ms: int | None = None,
) -> dict[str, Any]:
    """Add live-report provenance without turning incomplete evidence into pass."""

    path = Path(report_path)
    row = M.extract_metrics(response, dict(case), wall_time_ms=wall_time_ms)
    report = dict(live_report or {})
    status = str(report.get("status") or "missing")
    run_id = _run_id_from_report(report, path)
    artifact_gate = _artifact_gate_from_live_report(report, case)
    cad_test_gate_status = str(
        artifact_gate.get("cad_test_status") or "unavailable"
    ).casefold()
    native_delivery_verified = artifact_gate.get("native_delivery_verified") is True
    row.update(
        {
            "live_evaluation_run_id": run_id,
            "live_report_path": str(path),
            "live_report_status": status,
            "artifact_gate": artifact_gate,
            "cad_test_gate_status": cad_test_gate_status,
            "process_evaluation": _process_evaluation(report),
            "live_evaluation": _live_evaluation_snapshot(report, status=status),
        }
    )
    cad_test = report.get("cad_test")
    if isinstance(cad_test, Mapping) and cad_test:
        row["cad_test"] = dict(cad_test)
    cad_test_required = case.get("cad_test_required", True) is True
    is_2d = case.get("path") == "2d"
    row["passed"] = status == "finished" and artifact_gate["passed"] and (
        not cad_test_required
        or (
            cad_test_gate_status == "passed"
            and (is_2d or native_delivery_verified)
        )
    )
    if status != "finished":
        row["live_report_error"] = (
            "live report is not finished" if status != "missing" else "live report is missing"
        )
    return row


async def _wait_for_report(
    path: Path,
    *,
    timeout_seconds: float,
    poll_seconds: float = 0.1,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = None
            if isinstance(report, dict) and report.get("status") in {
                "finished",
                "failed",
                "blocked",
                "cancelled",
                "error",
            }:
                return report
        await asyncio.sleep(poll_seconds)
    return None


async def run_live_case(
    case: Mapping[str, Any],
    *,
    context: LiveBenchmarkContext,
    repeat_index: int,
    submitter: Submitter = submit_durable_workflow,
    waiter: Waiter = wait_for_compatibility_response,
    confirmer: Confirmer = confirm_mcad_workflow,
) -> dict[str, Any]:
    """Run one benchmark case through durable admission and live evidence."""

    case_id = str(case["id"])
    started = time.monotonic()
    submission = await submitter(
        context.principal,
        project_id=context.project_id,
        branch_id=context.branch_id,
        expected_base_revision_id=context.expected_base_revision_id,
        idempotency_key=f"benchmark:{context.run_id}:{case_id}:run-{repeat_index}",
        operation="generate",
        objective=str(case["description"]),
        output_formats=["dxf"] if case.get("path") == "2d" else ["step", "stl"],
        modeling_backend="auto",
        source_kind="benchmark",
        source_id=case_id,
        source_dataset_hash=context.dataset_hash,
        source_modeling_path=str(case.get("path") or "extrude_cut"),
    )
    response = await waiter(
        context.principal,
        submission,
        timeout_seconds=context.timeout_seconds,
    )
    if bool(getattr(response, "needs_confirmation", False)):
        await confirmer(
            submission.workflow_run_id,
            accepted=True,
            note="benchmark live observation",
            workflow_kind="mcad.agent.v2.generate",
        )
        response = await waiter(
            context.principal,
            submission,
            timeout_seconds=context.timeout_seconds,
        )
    workflow_id = str(submission.workflow_run_id)
    evaluation_run_id = evaluation_run_id_for_workflow(workflow_id)
    report_path = FileEvaluationStore(context.report_root).path_for(evaluation_run_id)
    report = await _wait_for_report(
        report_path,
        timeout_seconds=context.timeout_seconds,
    )
    return benchmark_row_from_live_report(
        case=case,
        response=response,
        live_report=report,
        report_path=report_path,
        wall_time_ms=int((time.monotonic() - started) * 1000),
    )
