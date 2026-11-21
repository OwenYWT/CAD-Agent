"""Benchmark adapter for the production durable execution path.

This module deliberately does not create a project, branch, revision, model,
or validation result. A live benchmark must be pointed at an existing workspace
and must consume the report written by the real API/Worker execution path.
"""

from __future__ import annotations

import asyncio
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


def _artifact_gate_from_live_report(
    report: Mapping[str, Any], case: Mapping[str, Any]
) -> dict[str, Any]:
    artifacts = _live_geometry_artifacts(report)
    formats = {
        str(artifact.get("format", "")).casefold(): artifact
        for artifact in artifacts
    }
    if case.get("path") == "2d":
        dxf_readable = formats.get("dxf", {}).get("parseable") is True
        return {
            "source": "live_report",
            "dxf_readable": dxf_readable,
            "passed": dxf_readable,
        }

    step_readable = formats.get("step", {}).get("parseable") is True
    stl_readable = formats.get("stl", {}).get("parseable") is True
    geometry_nonempty = _has_nonempty_live_geometry(artifacts)
    visual_rendered = _live_visual_rendered(report)
    return {
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
    row.update(
        {
            "live_evaluation_run_id": run_id,
            "live_report_path": str(path),
            "live_report_status": status,
            "artifact_gate": artifact_gate,
        }
    )
    row["passed"] = bool(row.get("passed")) and status == "finished" and artifact_gate["passed"]
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
        output_formats=["step", "stl"],
        modeling_backend="auto",
        source_kind="benchmark",
        source_id=case_id,
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
