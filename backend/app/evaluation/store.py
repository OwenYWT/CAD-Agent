"""Cross-process local store for live evaluation report snapshots."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts import EvaluationEvidence, LiveEventStatus, _validate_payload, utc_now
from .consistency import summarize_tool_calls
from .session import EvaluationSession
from .writer import markdown_path_for_json, report_path, write_report_payload


class FileEvaluationStore:
    def __init__(self, root: Path):
        self.root = root

    def path_for(self, evaluation_run_id: str) -> Path:
        return report_path(self.root, evaluation_run_id)

    def create(self, session: EvaluationSession) -> Path:
        target = self.path_for(session.evaluation_run_id)
        if target.exists():
            if not markdown_path_for_json(target).exists():
                self._write_human_report(self._read(target), target)
            return target
        return self._write(target, session.to_report())

    def append_event(
        self,
        evaluation_run_id: str,
        *,
        event_type: str,
        status: LiveEventStatus | str = LiveEventStatus.UNKNOWN,
        payload: dict[str, Any] | None = None,
        evidence_ids: tuple[str, ...] | list[str] = (),
        event_id: str | None = None,
    ) -> Path:
        target = self.path_for(evaluation_run_id)
        report = self._read(target)
        events = report.setdefault("timeline", [])
        event_identity = event_id or str(uuid4())
        if any(item.get("event_id") == event_identity for item in events):
            return target
        event_payload = dict(payload or {})
        events.append(
            {
                "event_id": event_identity,
                "event_type": event_type,
                "occurred_at": utc_now(),
                "sequence": len(events) + 1,
                "request_id": report.get("request_id"),
                "task_id": report.get("task_id"),
                "workflow_id": report.get("workflow_id"),
                "base_revision_id": report.get("base_revision_id"),
                "source_revision_id": report.get("source_revision_id"),
                "candidate_build_id": report.get("candidate_build_id"),
                "candidate_revision_id": report.get("candidate_revision_id"),
                "change_set_id": report.get("change_set_id"),
                "status": LiveEventStatus(status).value,
                "evidence_ids": list(evidence_ids),
                "payload": event_payload,
            }
        )
        duration_ms = event_payload.get("duration_ms")
        if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool):
            metrics = report.setdefault("metrics", {})
            metrics["total_duration_ms"] = int(metrics.get("total_duration_ms") or 0) + max(
                0, int(duration_ms)
            )
        return self._write(target, report)

    def add_evidence(self, evaluation_run_id: str, evidence: EvaluationEvidence) -> Path:
        target = self.path_for(evaluation_run_id)
        report = self._read(target)
        evidence_rows = report.setdefault("evidence", [])
        if not any(item.get("evidence_id") == evidence.evidence_id for item in evidence_rows):
            evidence_rows.append(evidence.to_dict())
            return self._write(target, report)
        return target

    def append_tool_call(
        self,
        evaluation_run_id: str,
        call: dict[str, Any],
    ) -> Path:
        _validate_payload(call)
        target = self.path_for(evaluation_run_id)
        report = self._read(target)
        calls = report.setdefault("tool_calls", [])
        call_id = str(call.get("call_id") or "")
        if call_id and any(item.get("call_id") == call_id for item in calls):
            return target
        stored = dict(call)
        stored.setdefault("sequence", len(calls) + 1)
        calls.append(stored)
        metrics = report.setdefault("metrics", {})
        metrics["total_call_count"] = len(calls)
        kind = str(stored.get("call_kind") or "unknown")
        metric_key = {
            "workflow_activity": "activity_count",
            "model_tool": "model_tool_count",
            "cad_operation": "cad_operation_count",
        }.get(kind, f"{kind}_count")
        metrics[metric_key] = int(metrics.get(metric_key) or 0) + 1
        if str(stored.get("status") or "").lower() in {"error", "failed", "fail"}:
            metrics["failed_call_count"] = int(metrics.get("failed_call_count") or 0) + 1
        if str(stored.get("name") or "").startswith("agent_v2.repair_"):
            metrics["repair_call_count"] = int(metrics.get("repair_call_count") or 0) + 1
        if bool(stored.get("replayed")) or int(stored.get("attempt") or 1) > 1:
            metrics["retry_call_count"] = int(metrics.get("retry_call_count") or 0) + 1
        report["consistency"] = summarize_tool_calls(calls)
        return self._write(target, report)

    def update_identity(self, evaluation_run_id: str, **identity: str | None) -> Path:
        target = self.path_for(evaluation_run_id)
        report = self._read(target)
        allowed = {
            "request_id",
            "task_id",
            "workflow_id",
            "base_revision_id",
            "source_revision_id",
            "candidate_build_id",
            "candidate_revision_id",
            "change_set_id",
        }
        unknown = set(identity).difference(allowed)
        if unknown:
            raise ValueError(f"unsupported evaluation identity: {sorted(unknown)}")
        for key, value in identity.items():
            if value is not None:
                report[key] = value
        return self._write(target, report)

    def finish(self, evaluation_run_id: str, status: str = "finished") -> Path:
        target = self.path_for(evaluation_run_id)
        report = self._read(target)
        report["status"] = status
        report["finished_at"] = utc_now()
        return self._write(target, report)

    @staticmethod
    def _read(target: Path) -> dict[str, Any]:
        return json.loads(target.read_text(encoding="utf-8"))

    @staticmethod
    def _write(target: Path, payload: dict[str, Any]) -> Path:
        return write_report_payload(payload, target)

    @staticmethod
    def _write_human_report(payload: dict[str, Any], target: Path) -> Path:
        from .human_report import render_human_report

        markdown_target = target.with_suffix(".md")
        markdown_target.parent.mkdir(parents=True, exist_ok=True)
        temporary = markdown_target.with_name(f".{markdown_target.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                render_human_report(payload), encoding="utf-8", newline="\n"
            )
            temporary.replace(markdown_target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return markdown_target
