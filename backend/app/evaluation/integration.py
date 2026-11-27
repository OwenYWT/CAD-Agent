"""Best-effort adapters from real workflow results to live evaluation events."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from functools import wraps
from typing import Any, Awaitable, Callable, Mapping

from app.model_job_context import model_job_context

from .contracts import EvaluationEvidence, EvaluationMode, EvaluationStatus, LiveEventStatus, utc_now
from .cad_test_evidence import normalize_cad_test_result
from .cad_test_runner import CadTestRunnerConfig, run_cad_test, run_cad_test_bundle
from .mode import resolve_mode
from .session import EvaluationSession
from .store import FileEvaluationStore

logger = logging.getLogger(__name__)


def default_report_root() -> Path:
    if configured := os.environ.get("CAD_EVALUATION_REPORT_ROOT"):
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "benchmark" / "reports"


def evaluation_run_id_for_workflow(workflow_run_id: object) -> str:
    return f"live-{workflow_run_id}"


def _workflow_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("workflow_run_id")
    return str(value) if value is not None else None


def _model_operation_event_id(
    activity_name: str,
    payload: Mapping[str, Any],
) -> str:
    workflow_id = _workflow_id(payload) or "unknown-workflow"
    job = model_job_context.get()
    if job is not None:
        return (
            f"{workflow_id}:{activity_name}:{job.job_id}:"
            f"generation-{job.generation}"
        )
    attempt = str(
        payload.get("attempt_id")
        or payload.get("execution_attempt_id")
        or payload.get("step_key")
        or "default"
    )
    suffix = hashlib.sha256(
        f"{activity_name}:{attempt}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{workflow_id}:{activity_name}:evaluation:{suffix}"


def observe_model_operation(
    activity_name: str,
    operation: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Observe the real leased model operation without changing its contract."""

    @wraps(operation)
    async def observed(payload: dict[str, Any]) -> dict[str, Any]:
        event_id = _model_operation_event_id(activity_name, payload)
        started_at = utc_now()
        started_monotonic = time.perf_counter()
        try:
            result = await operation(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                record_activity_failure(
                    payload,
                    activity_name,
                    exc,
                    event_id=event_id,
                    started_at=started_at,
                    finished_at=utc_now(),
                    duration_ms=max(0, round((time.perf_counter() - started_monotonic) * 1000)),
                )
            except Exception:
                pass
            raise
        try:
            record_activity_result(
                payload,
                activity_name,
                result,
                event_id=event_id,
                started_at=started_at,
                finished_at=utc_now(),
                duration_ms=max(0, round((time.perf_counter() - started_monotonic) * 1000)),
            )
        except Exception:
            pass
        return result

    return observed


def ensure_live_session(
    payload: Mapping[str, Any],
    *,
    root: Path | None = None,
    source_kind: str = "user_task",
    source_id: str | None = None,
    source_metadata: Mapping[str, str] | None = None,
) -> str | None:
    if resolve_mode() is not EvaluationMode.OBSERVE:
        return None
    workflow_id = _workflow_id(payload)
    if workflow_id is None:
        return None
    run_id = evaluation_run_id_for_workflow(workflow_id)
    store = FileEvaluationStore(root or default_report_root())
    session = EvaluationSession.create(
        mode=EvaluationMode.OBSERVE,
        evaluation_run_id=run_id,
        source_kind=source_kind,
        source_id=source_id,
        source_metadata=source_metadata,
        request_id=str(payload.get("request_id") or workflow_id),
        task_id=str(payload.get("task_id") or workflow_id),
        workflow_id=workflow_id,
        base_revision_id=(
            str(payload["expected_base_revision_id"])
            if payload.get("expected_base_revision_id") is not None
            else None
        ),
        source_revision_id=(
            str((payload.get("revision_restore") or {}).get("source_revision_id"))
            if isinstance(payload.get("revision_restore"), Mapping)
            and (payload.get("revision_restore") or {}).get("source_revision_id") is not None
            else None
        ),
    )
    try:
        first_create = not store.path_for(run_id).exists()
        store.create(session)
        if first_create:
            revision_restore = payload.get("revision_restore")
            operation_context = payload.get("operation_context")
            store.append_event(
                run_id,
                event_id=f"{workflow_id}:session_started",
                event_type="session_started",
                payload={
                    "operation": str(payload.get("operation") or "unknown"),
                    "objective": str(payload.get("objective") or "")[:4000],
                    "restore_requested": isinstance(revision_restore, Mapping),
                    **(
                        {
                            "restore_source_revision_id": str(
                                revision_restore["source_revision_id"]
                            )
                        }
                        if isinstance(revision_restore, Mapping)
                        and revision_restore.get("source_revision_id") is not None
                        else {}
                    ),
                    **(
                        {"operation_context_rule": str(operation_context["rule"])}
                        if isinstance(operation_context, Mapping)
                        and operation_context.get("rule") is not None
                        else {}
                    ),
                },
            )
        return run_id
    except Exception as exc:
        logger.warning("live evaluation session initialization failed: %s", type(exc).__name__)
        return None


def _status(result: Mapping[str, Any]) -> LiveEventStatus:
    value = str(result.get("outcome") or result.get("status") or "").lower()
    if value in {"pass", "passed", "success", "succeeded", "completed"}:
        return LiveEventStatus.PASS
    if value in {"fail", "failed", "error", "rejected"}:
        return LiveEventStatus.FAIL
    if value in {"blocked", "unavailable"}:
        return LiveEventStatus.BLOCKED
    if value in {"skipped", "not_applicable"}:
        return LiveEventStatus.SKIPPED
    return LiveEventStatus.UNKNOWN


def _event_type(activity_name: str) -> str:
    return {
        "agent_v2.requirements": "requirement_parsed",
        "agent_v2.decompose": "obligations_bound",
        "agent_v2.plan": "plan_created",
        "agent_v2.generate_source": "operation_finished",
        "agent_v2.repair_source": "operation_finished",
        "agent_v2.generate_operations": "operation_finished",
        "agent_v2.repair_operations": "operation_finished",
        "agent_v2.execute_model": "operation_finished",
        "agent_v2.execute_freecad": "operation_finished",
        "agent_v2.validate_geometry": "geometry_validation_finished",
        "agent_v2.render_visual": "visual_validation_finished",
        "agent_v2.judge_visual": "visual_validation_finished",
        "agent_v2.repair_visual": "visual_validation_finished",
        "agent_v2.validate_dfm": "dfm_validation_finished",
        "agent_v2.generate_bom": "bom_validation_finished",
        "agent_v2.seal_candidate": "candidate_sealed",
    }.get(activity_name, "operation_finished")


def _event_type_for_result(
    activity_name: str,
    result: Mapping[str, Any],
) -> str:
    if activity_name == "mcad.finalize":
        if result.get("committed") is True:
            return "revision_committed"
        if result.get("committed") is False:
            return "change_set_created"
    return _event_type(activity_name)


def _safe_summary(activity_name: str, result: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "status",
        "gate",
        "mode",
        "outcome",
        "evidence_id",
        "evidence_hash",
        "attempt_id",
        "step_key",
        "candidate_build_id",
        "candidate_revision_id",
        "change_set_id",
        "replayed",
    }
    summary = {key: result[key] for key in allowed if key in result}
    summary["activity"] = activity_name
    return summary


def _runtime_summary(
    *,
    started_at: str | None,
    finished_at: str | None,
    duration_ms: int | None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if started_at is not None:
        summary["started_at"] = str(started_at)
    if finished_at is not None:
        summary["finished_at"] = str(finished_at)
    if duration_ms is not None:
        summary["duration_ms"] = max(0, int(duration_ms))
    return summary


def _evaluation_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tool_call_status(value: Any, *, failure: bool = False) -> str:
    if failure:
        return "error"
    normalized = str(value or "").lower()
    if normalized in {"pass", "passed", "success", "succeeded", "completed"}:
        return "passed"
    if normalized in {"fail", "failed", "error", "rejected"}:
        return "failed"
    if normalized in {"running", "in_progress", "pending", "building"}:
        return "running"
    if normalized in {"observed", "rendered"}:
        return "observed"
    return normalized or "unknown"


def _usage_count(value: Any) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    return int(value)


def _safe_model_usage(provenance: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(provenance, Mapping):
        return None
    usage = provenance.get("usage")
    if not isinstance(usage, Mapping):
        return None
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, Mapping) else {}
    projected = {
        "unit": "token",
        "input": _usage_count(usage.get("prompt_tokens")),
        "output": _usage_count(usage.get("completion_tokens")),
        "total": _usage_count(usage.get("total_tokens")),
        "reasoning": _usage_count(details.get("reasoning_tokens")),
    }
    measured = {key: value for key, value in projected.items() if value is not None}
    return measured if len(measured) > 1 else None


def _activity_tool_call(
    *,
    payload: Mapping[str, Any],
    activity_name: str,
    result: Mapping[str, Any] | None,
    error: Exception | None,
    event_id: str | None,
    started_at: str | None,
    finished_at: str | None,
    duration_ms: int | None,
    evidence_ids: list[str] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    safe_input = {
        key: value
        for key, value in payload.items()
        if key not in {"workflow_run_id", "task_id", "request_id"}
        and not any(part in str(key).lower() for part in ("token", "secret", "password", "credential", "api_key"))
    }
    output = _safe_summary(activity_name, result or {}) if result is not None else {
        "error_type": type(error).__name__ if error else "Error",
        "error_message": str(error)[:1000] if error else "unknown failure",
    }
    call = {
        "call_id": event_id or _evaluation_hash({"activity": activity_name, "payload": safe_input}),
        "call_kind": "workflow_activity",
        "name": activity_name,
        "status": _tool_call_status(
            (result or {}).get("outcome") or (result or {}).get("status"),
            failure=error is not None,
        ),
        "attempt": max(1, int(payload.get("attempt") or payload.get("activity_attempt") or 1)),
        "replayed": bool((result or {}).get("replayed") or payload.get("replayed")),
        "input_hash": _evaluation_hash(safe_input),
        "output_hash": _evaluation_hash(output),
    }
    call.update(_runtime_summary(started_at=started_at, finished_at=finished_at, duration_ms=duration_ms))
    if evidence_ids:
        call["evidence_ids"] = list(evidence_ids)
    model_usage = _safe_model_usage(provenance)
    if model_usage is not None:
        call["model_usage"] = model_usage
    return call


def _nested_tool_calls(
    *,
    activity_name: str,
    provenance: Mapping[str, Any],
    parent_call_id: str | None,
) -> list[dict[str, Any]]:
    raw_calls: list[Mapping[str, Any]] = []
    for item in provenance.get("tool_calls") or ():
        if not isinstance(item, Mapping):
            continue
        raw_calls.append(item)
    execution = provenance.get("execution_tool_call")
    if isinstance(execution, Mapping):
        raw_calls.append({"call": execution})
    projected = []
    for index, item in enumerate(raw_calls, start=1):
        raw_call = item.get("call") if isinstance(item.get("call"), Mapping) else item
        function = raw_call.get("function") if isinstance(raw_call, Mapping) else None
        function = function if isinstance(function, Mapping) else {}
        name = str(function.get("name") or raw_call.get("name") or "unknown_tool")
        arguments = function.get("arguments")
        arguments_hash = raw_call.get("arguments_hash")
        if not isinstance(arguments_hash, str) or len(arguments_hash) != 64:
            arguments_hash = _evaluation_hash(arguments if arguments is not None else raw_call)
        result = item.get("result") if isinstance(item.get("result"), Mapping) else None
        projected.append(
            {
                "call_id": f"{parent_call_id or activity_name}:nested-{index}",
                "call_kind": "cad_operation" if name.startswith("freecad_") else "model_tool",
                "name": name,
                "status": _tool_call_status(result.get("status") if result else None) if result else "observed",
                "attempt": 1,
                "replayed": False,
                "parent_activity": activity_name,
                "input_hash": arguments_hash,
                "output_hash": _evaluation_hash(result) if result is not None else None,
            }
        )
    return projected


_MODEL_ACTIVITY_NAMES = {
    "agent_v2.requirements",
    "agent_v2.plan",
    "agent_v2.generate_source",
    "agent_v2.repair_source",
    "agent_v2.generate_operations",
    "agent_v2.repair_operations",
    "agent_v2.judge_visual",
    "agent_v2.repair_visual",
}


def _provenance_for_result(
    activity_name: str,
    result: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    provenance = result.get("provenance")
    if isinstance(provenance, Mapping):
        return provenance
    if activity_name not in _MODEL_ACTIVITY_NAMES:
        return None
    try:
        from app.llm import get_last_chat_completion_provenance

        current = get_last_chat_completion_provenance()
    except Exception:
        return None
    return current if isinstance(current, Mapping) else None


def _evidence_from_result(activity_name: str, result: Mapping[str, Any]) -> EvaluationEvidence | None:
    evidence_id = result.get("evidence_id")
    if not evidence_id:
        return None
    report = result.get("report")
    observed = report if isinstance(report, Mapping) else _safe_summary(activity_name, result)
    digest = result.get("evidence_hash")
    digest = digest if isinstance(digest, str) and len(digest) == 64 else None
    return EvaluationEvidence(
        evidence_id=str(evidence_id),
        source_kind=activity_name,
        source_ref=str(evidence_id),
        authority="runtime_validation",
        artifact_sha256=digest,
        observed=observed,
    )


def _identity_from_activity(
    payload: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, str]:
    identity: dict[str, str] = {}
    for key in (
        "base_revision_id",
        "source_revision_id",
        "candidate_build_id",
        "candidate_revision_id",
        "change_set_id",
    ):
        value = result.get(key)
        if value is None:
            value = payload.get(key)
        if value is not None:
            identity[key] = str(value)
    return identity


def record_activity_result(
    payload: Mapping[str, Any],
    activity_name: str,
    result: Mapping[str, Any],
    *,
    root: Path | None = None,
    event_id: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_ms: int | None = None,
) -> str | None:
    run_id = ensure_live_session(payload, root=root)
    if run_id is None:
        return None
    store = FileEvaluationStore(root or default_report_root())
    evidence = _evidence_from_result(activity_name, result)
    provenance = _provenance_for_result(activity_name, result)
    try:
        if evidence is not None:
            store.add_evidence(run_id, evidence)
        identity = _identity_from_activity(payload, result)
        if identity:
            store.update_identity(run_id, **identity)
        store.append_event(
            run_id,
            event_id=event_id,
            event_type=_event_type_for_result(activity_name, result),
            status=_status(result),
            evidence_ids=[evidence.evidence_id] if evidence else [],
            payload={
                **_safe_summary(activity_name, result),
                **_runtime_summary(
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                ),
            },
        )
        store.append_tool_call(
            run_id,
            _activity_tool_call(
                payload=payload,
                activity_name=activity_name,
                result=result,
                error=None,
                event_id=event_id,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                evidence_ids=[evidence.evidence_id] if evidence else None,
                provenance=provenance,
            ),
        )
        if isinstance(provenance, Mapping):
            for tool_call in _nested_tool_calls(
                activity_name=activity_name,
                provenance=provenance,
                parent_call_id=event_id,
            ):
                store.append_tool_call(run_id, tool_call)
    except Exception as exc:
        logger.warning("live evaluation activity recording failed: %s", type(exc).__name__)
    return run_id


def record_activity_failure(
    payload: Mapping[str, Any],
    activity_name: str,
    error: Exception,
    *,
    root: Path | None = None,
    event_id: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_ms: int | None = None,
) -> str | None:
    run_id = ensure_live_session(payload, root=root)
    if run_id is None:
        return None
    store = FileEvaluationStore(root or default_report_root())
    try:
        store.append_event(
            run_id,
            event_id=event_id,
            event_type="operation_failed",
            status=LiveEventStatus.ERROR,
            payload={
                "activity": activity_name,
                "error_type": type(error).__name__,
                "error_message": str(error)[:1000],
                **_runtime_summary(
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                ),
            },
        )
        store.append_tool_call(
            run_id,
            _activity_tool_call(
                payload=payload,
                activity_name=activity_name,
                result=None,
                error=error,
                event_id=event_id,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
            ),
        )
    except Exception as exc:
        logger.warning("live evaluation failure recording failed: %s", type(exc).__name__)
    return run_id


def record_workflow_terminal(
    payload: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    root: Path | None = None,
) -> str | None:
    run_id = ensure_live_session(payload, root=root)
    if run_id is None:
        return None
    raw_status = str(result.get("status") or "").lower()
    status = {
        "succeeded": EvaluationStatus.FINISHED.value,
        "cancelled": EvaluationStatus.CANCELLED.value,
        "failed": EvaluationStatus.FAILED.value,
        "timed_out": EvaluationStatus.FAILED.value,
    }.get(raw_status)
    if status is None:
        return None
    store = FileEvaluationStore(root or default_report_root())
    event_status = {
        "succeeded": LiveEventStatus.PASS,
        "failed": LiveEventStatus.FAIL,
        "timed_out": LiveEventStatus.FAIL,
        "cancelled": LiveEventStatus.UNKNOWN,
    }[raw_status]
    try:
        store.append_event(
            run_id,
            event_id=f"{run_id}:session-finished",
            event_type="session_finished",
            status=event_status,
            payload={"business_status": raw_status},
        )
    except Exception as exc:
        logger.warning("live evaluation terminal event failed: %s", type(exc).__name__)
    try:
        store.finish(run_id, status)
    except Exception as exc:
        logger.warning("live evaluation finalization failed: %s", type(exc).__name__)
    return run_id


def record_change_set_transition(
    payload: Mapping[str, Any],
    action: str,
    result: Any,
    *,
    root: Path | None = None,
) -> str | None:
    run_id = ensure_live_session(payload, root=root)
    if run_id is None:
        return None
    event_type = {
        "accepted": "change_set_reviewed",
        "committed": "revision_committed",
        "rejected": "change_set_rejected",
        "changes_requested": "change_set_reviewed",
        "rolled_back": "revision_rolled_back",
    }.get(action)
    if event_type is None:
        raise ValueError(f"unsupported change set transition: {action}")
    if isinstance(result, Mapping):
        change_set_id = result.get("change_set_id")
        status = result.get("status")
        replayed = result.get("replayed")
    else:
        change_set_id = getattr(result, "change_set_id", None)
        status = getattr(result, "status", None)
        replayed = getattr(result, "replayed", None)
    try:
        store = FileEvaluationStore(root or default_report_root())
        if change_set_id is not None:
            store.update_identity(run_id, change_set_id=str(change_set_id))
        store.append_event(
            run_id,
            event_id=(
                f"{run_id}:change-set:{change_set_id or 'unknown'}:{action}"
            ),
            event_type=event_type,
            payload={
                "status": str(status or action),
                "replayed": bool(replayed),
            },
        )
        if action in {
            "committed",
            "rejected",
            "changes_requested",
            "rolled_back",
        }:
            store.append_event(
                run_id,
                event_id=f"{run_id}:session-finished",
                event_type="session_finished",
                status=(
                    LiveEventStatus.PASS
                    if action == "committed"
                    else LiveEventStatus.UNKNOWN
                ),
                payload={"business_status": action},
            )
            store.finish(run_id, EvaluationStatus.FINISHED.value)
    except Exception as exc:
        logger.warning("live evaluation Change Set recording failed: %s", type(exc).__name__)
    return run_id


def _cad_test_source_identity(
    store: FileEvaluationStore,
    run_id: str,
) -> Mapping[str, Any]:
    try:
        report = json.loads(store.path_for(run_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    source = report.get("source")
    return source if isinstance(source, Mapping) else {}


def _cad_test_identity_block(
    *,
    source: Mapping[str, Any],
    case_id: str,
    workflow_id: str,
    candidate_revision_id: str,
    dataset_hash: str | None,
    dataset_configured: bool,
    worker_build_id: str | None = None,
) -> dict[str, Any] | None:
    reason = None
    if source.get("kind") != "benchmark":
        reason = "cad_test_source_identity_mismatch"
    elif str(source.get("id") or "") != case_id:
        reason = "cad_test_case_identity_mismatch"
    elif dataset_configured and dataset_hash is None:
        reason = "cad_test_dataset_integrity_unavailable"
    elif dataset_hash is not None and not source.get("dataset_hash"):
        reason = "cad_test_dataset_identity_missing"
    elif dataset_hash is not None and source.get("dataset_hash") != dataset_hash:
        reason = "cad_test_dataset_identity_mismatch"
    elif source.get("expected_worker_build_id") and (
        worker_build_id != source.get("expected_worker_build_id")
    ):
        reason = "cad_test_worker_identity_mismatch"
    if reason is None:
        return None
    return {
        "status": "blocked",
        "reason": reason,
        "case_id": case_id,
        "dataset_hash": dataset_hash,
        "workflow_id": workflow_id,
        "candidate_revision_id": candidate_revision_id,
        "expected_worker_build_id": source.get("expected_worker_build_id"),
        "worker_build_id": worker_build_id,
    }


def _record_blocked_cad_test_result(
    store: FileEvaluationStore,
    run_id: str,
    result: Mapping[str, Any],
    *,
    source: Mapping[str, Any],
) -> None:
    store.attach_cad_test_result(
        run_id,
        result,
        expected_case_id=str(source.get("id") or "") or None,
        expected_dataset_hash=(
            str(source.get("dataset_hash") or "") or None
            if result.get("dataset_hash") is not None
            else None
        ),
    )
    store.append_event(
        run_id,
        event_id=f"{run_id}:cad-test:{result.get('case_id')}",
        event_type="cad_test_finished",
        status=LiveEventStatus.BLOCKED,
        payload={
            "case_id": result.get("case_id"),
            "status": "blocked",
            "reason": result.get("reason"),
        },
    )


def _score_sealed_artifacts(
    *,
    store: FileEvaluationStore,
    run_id: str,
    source: Mapping[str, Any],
    artifacts: Mapping[str, Path],
    case_id: str,
    workflow_id: str,
    candidate_revision_id: str,
    config: CadTestRunnerConfig,
    dataset_hash: str | None,
    bundle: bool,
    modeling_path: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    identity = dict(case_id=case_id, workflow_id=workflow_id,
                    candidate_revision_id=candidate_revision_id)

    def blocked(reason: str) -> tuple[None, None]:
        _record_blocked_cad_test_result(
            store, run_id,
            {**identity, "status": "blocked", "reason": reason, "dataset_hash": dataset_hash},
            source=source,
        )
        return None, None

    route = str(source.get("modeling_path") or "extrude_cut")
    if modeling_path is not None and modeling_path != route:
        return blocked("cad_test_modeling_path_mismatch")
    primary = "dxf" if route == "2d" else "step"
    required = {primary} if primary == "dxf" or not bundle else {"step", "fcstd", "stl"}
    if set(artifacts) != required:
        return blocked("required_delivery_artifacts_missing")
    seal = store.cad_test_seal(run_id)
    if not candidate_revision_id or seal.get("candidate_revision_id") != candidate_revision_id:
        return blocked("cad_test_sealed_artifact_identity_unavailable")
    sealed_artifacts = seal.get("artifacts")
    if not isinstance(sealed_artifacts, Mapping):
        return blocked("cad_test_sealed_artifact_identity_unavailable")
    for kind in required:
        facts = sealed_artifacts.get(kind)
        if (
            not isinstance(facts, Mapping)
            or not re.fullmatch(r"[a-f0-9]{64}", str(facts.get("sha256") or ""))
            or type(facts.get("size_bytes")) is not int
            or facts["size_bytes"] <= 0
        ):
            return blocked("cad_test_sealed_artifact_identity_unavailable")

    config.root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cad_test_sealed_", dir=config.root) as temporary:
        staged = {}
        for kind in sorted(required):
            path = Path(artifacts[kind])
            if not path.is_file():
                return blocked("artifact_not_found")
            destination = Path(temporary) / f"result.{kind}"
            facts = sealed_artifacts[kind]
            try:
                if path.stat().st_size != facts["size_bytes"]:
                    return blocked("cad_test_artifact_identity_mismatch")
                shutil.copyfile(path, destination)
                with destination.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest != facts["sha256"] or destination.stat().st_size != facts["size_bytes"]:
                    return blocked("cad_test_artifact_identity_mismatch")
            except OSError:
                return blocked("cad_test_artifact_unreadable")
            staged[kind] = destination
        if bundle or primary == "dxf":
            result = run_cad_test_bundle(artifacts=staged, modeling_path=route, config=config, **identity)
        else:
            result = run_cad_test(artifact_path=staged[primary], config=config, **identity)
    if result is not None and result.get("status") in {"evaluation_error", "error"}:
        result.setdefault("dataset_hash", dataset_hash)
    return result, str(sealed_artifacts[primary]["sha256"])


def attach_cad_test_result(
    payload: Mapping[str, Any],
    artifact_path: Path,
    *,
    case_id: str,
    root: Path | None = None,
    config: CadTestRunnerConfig,
) -> str | None:
    """Score an authoritative artifact and attach only bounded evidence."""

    run_id = ensure_live_session(payload, root=root)
    if run_id is None:
        return None
    workflow_id = _workflow_id(payload) or ""
    candidate_revision_id = str(payload.get("candidate_revision_id") or "")
    store = FileEvaluationStore(root or default_report_root())
    source = _cad_test_source_identity(store, run_id)
    dataset_hash = config.resolved_dataset_hash()
    blocked = _cad_test_identity_block(
        source=source,
        case_id=case_id,
        workflow_id=workflow_id,
        candidate_revision_id=candidate_revision_id,
        dataset_hash=dataset_hash,
        dataset_configured=config.dataset_root is not None,
        worker_build_id=os.environ.get("TEMPORAL_WORKER_BUILD_ID") or None,
    )
    if blocked is not None:
        _record_blocked_cad_test_result(store, run_id, blocked, source=source)
        return run_id
    primary = "dxf" if source.get("modeling_path") == "2d" else "step"
    result, expected_artifact_sha256 = _score_sealed_artifacts(
        store=store, run_id=run_id, source=source, dataset_hash=dataset_hash,
        artifacts={primary: artifact_path}, bundle=False,
        case_id=case_id,
        workflow_id=workflow_id,
        candidate_revision_id=candidate_revision_id,
        config=config,
    )
    if result is None:
        return run_id
    result = {
        **result,
        "expected_worker_build_id": source.get("expected_worker_build_id"),
        "worker_build_id": os.environ.get("TEMPORAL_WORKER_BUILD_ID") or None,
    }
    normalized = normalize_cad_test_result(
        result,
        expected_workflow_id=workflow_id,
        expected_candidate_revision_id=(candidate_revision_id or None),
        expected_artifact_sha256=expected_artifact_sha256,
        expected_case_id=str(source.get("id") or "") or None,
        expected_dataset_hash=str(source.get("dataset_hash") or "") or None,
    )
    try:
        store.attach_cad_test_result(
            run_id,
            result,
            expected_artifact_sha256=expected_artifact_sha256,
            expected_case_id=str(source.get("id") or "") or None,
            expected_dataset_hash=str(source.get("dataset_hash") or "") or None,
        )
        status = {
            "passed": LiveEventStatus.PASS,
            "failed": LiveEventStatus.FAIL,
            "error": LiveEventStatus.ERROR,
            "blocked": LiveEventStatus.BLOCKED,
            "unknown": LiveEventStatus.UNKNOWN,
            "unavailable": LiveEventStatus.UNKNOWN,
        }.get(str(normalized["status"]), LiveEventStatus.UNKNOWN)
        store.append_event(
            run_id,
            event_id=f"{run_id}:cad-test:{case_id}",
            event_type="cad_test_finished",
            status=status,
            payload={
                "case_id": case_id,
                "status": normalized["status"],
                "reason": normalized["reason"],
            },
        )
    except Exception as exc:
        logger.warning("live evaluation CADTest recording failed: %s", type(exc).__name__)
        raise
    return run_id


def attach_cad_test_artifacts(
    payload: Mapping[str, Any],
    artifacts: Mapping[str, Path],
    *,
    case_id: str,
    modeling_path: str | None = None,
    root: Path | None = None,
    config: CadTestRunnerConfig,
) -> str | None:
    """Attach independent scoring and native-delivery evidence for one bundle."""

    run_id = ensure_live_session(payload, root=root)
    if run_id is None:
        return None
    workflow_id = _workflow_id(payload) or ""
    candidate_revision_id = str(payload.get("candidate_revision_id") or "")
    store = FileEvaluationStore(root or default_report_root())
    source = _cad_test_source_identity(store, run_id)
    dataset_hash = config.resolved_dataset_hash()
    blocked = _cad_test_identity_block(
        source=source,
        case_id=case_id,
        workflow_id=workflow_id,
        candidate_revision_id=candidate_revision_id,
        dataset_hash=dataset_hash,
        dataset_configured=config.dataset_root is not None,
        worker_build_id=os.environ.get("TEMPORAL_WORKER_BUILD_ID") or None,
    )
    if blocked is not None:
        _record_blocked_cad_test_result(store, run_id, blocked, source=source)
        return run_id
    result, expected_artifact_sha256 = _score_sealed_artifacts(
        store=store, run_id=run_id, source=source, dataset_hash=dataset_hash, bundle=True,
        artifacts=artifacts,
        case_id=case_id,
        workflow_id=workflow_id,
        candidate_revision_id=candidate_revision_id,
        config=config,
        modeling_path=modeling_path,
    )
    if result is None:
        return run_id
    result = {
        **result,
        "expected_worker_build_id": source.get("expected_worker_build_id"),
        "worker_build_id": os.environ.get("TEMPORAL_WORKER_BUILD_ID") or None,
    }
    normalized = normalize_cad_test_result(
        result,
        expected_workflow_id=workflow_id,
        expected_candidate_revision_id=(candidate_revision_id or None),
        expected_artifact_sha256=expected_artifact_sha256,
        expected_case_id=str(source.get("id") or "") or None,
        expected_dataset_hash=str(source.get("dataset_hash") or "") or None,
    )
    try:
        store.attach_cad_test_result(
            run_id,
            result,
            expected_artifact_sha256=expected_artifact_sha256,
            expected_case_id=str(source.get("id") or "") or None,
            expected_dataset_hash=str(source.get("dataset_hash") or "") or None,
        )
        status = {
            "passed": LiveEventStatus.PASS,
            "failed": LiveEventStatus.FAIL,
            "error": LiveEventStatus.ERROR,
            "blocked": LiveEventStatus.BLOCKED,
            "unknown": LiveEventStatus.UNKNOWN,
            "unavailable": LiveEventStatus.UNKNOWN,
        }.get(str(normalized["status"]), LiveEventStatus.UNKNOWN)
        store.append_event(
            run_id,
            event_id=f"{run_id}:cad-test:{case_id}",
            event_type="cad_test_finished",
            status=status,
            payload={
                "case_id": case_id,
                "status": normalized["status"],
                "reason": normalized["reason"],
            },
        )
    except Exception as exc:
        logger.warning("live evaluation CADTest bundle recording failed: %s", type(exc).__name__)
        raise
    return run_id


def attach_cad_test_observation(
    payload: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    root: Path | None = None,
) -> str | None:
    """Attach an evidence-only CADTest observation without running a scorer."""

    run_id = ensure_live_session(payload, root=root)
    if run_id is None:
        return None
    workflow_id = _workflow_id(payload) or ""
    candidate_revision_id = str(payload.get("candidate_revision_id") or "")
    store = FileEvaluationStore(root or default_report_root())
    try:
        source = _cad_test_source_identity(store, run_id)
        if source.get("kind") != "user_task" or result.get("status") != "not_applicable":
            status = result.get("status")
            result = {
                "workflow_id": result.get("workflow_id"),
                "candidate_revision_id": result.get("candidate_revision_id"),
                "case_id": result.get("case_id"),
                "expected_worker_build_id": result.get("expected_worker_build_id"),
                "worker_build_id": result.get("worker_build_id"),
                "status": status if status in ("blocked", "error") else "blocked",
                "reason": (
                    result.get("reason") if status in ("blocked", "error")
                    else "cad_test_trusted_scorer_required"
                ),
            }
        normalized = normalize_cad_test_result(
            result,
            expected_workflow_id=workflow_id,
            expected_candidate_revision_id=(candidate_revision_id or None),
        )
        store.attach_cad_test_result(run_id, result)
        status = {
            "passed": LiveEventStatus.PASS,
            "failed": LiveEventStatus.FAIL,
            "error": LiveEventStatus.ERROR,
            "blocked": LiveEventStatus.BLOCKED,
            "unknown": LiveEventStatus.UNKNOWN,
            "unavailable": LiveEventStatus.UNKNOWN,
            "not_applicable": LiveEventStatus.SKIPPED,
        }.get(str(normalized["status"]), LiveEventStatus.UNKNOWN)
        case = result.get("case_id")
        event_case = str(case) if case else "not-applicable"
        store.append_event(
            run_id,
            event_id=f"{run_id}:cad-test:{event_case}",
            event_type="cad_test_finished",
            status=status,
            payload={
                "case_id": case,
                "status": normalized["status"],
                "reason": normalized["reason"],
            },
        )
    except Exception as exc:
        logger.warning("live evaluation CADTest observation recording failed: %s", type(exc).__name__)
        raise
    return run_id
