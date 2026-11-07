"""Best-effort adapters from real workflow results to live evaluation events."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from functools import wraps
from typing import Any, Awaitable, Callable, Mapping

from app.model_job_context import model_job_context

from .contracts import EvaluationEvidence, EvaluationMode, EvaluationStatus, LiveEventStatus, utc_now
from .mode import resolve_mode
from .session import EvaluationSession
from .store import FileEvaluationStore

logger = logging.getLogger(__name__)


def default_report_root() -> Path:
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
            ),
        )
        provenance = _provenance_for_result(activity_name, result)
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
