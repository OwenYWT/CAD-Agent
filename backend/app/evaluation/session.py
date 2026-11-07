"""In-memory live evaluation session and event lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4
from typing import Any, Mapping

from .contracts import (
    EvaluationEvidence,
    EvaluationEvent,
    EvaluationMode,
    EvaluationReport,
    EvaluationStatus,
    LiveEventStatus,
    utc_now,
)


_TERMINAL_STATUSES = {
    EvaluationStatus.FINISHED,
    EvaluationStatus.FAILED,
    EvaluationStatus.BLOCKED,
    EvaluationStatus.CANCELLED,
    EvaluationStatus.ERROR,
}


@dataclass
class EvaluationSession:
    evaluation_run_id: str
    mode: EvaluationMode
    source: dict[str, str]
    started_at: str
    status: EvaluationStatus = EvaluationStatus.RUNNING
    finished_at: str | None = None
    request_id: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    base_revision_id: str | None = None
    source_revision_id: str | None = None
    candidate_build_id: str | None = None
    candidate_revision_id: str | None = None
    change_set_id: str | None = None
    _events: list[EvaluationEvent] = field(default_factory=list, repr=False)
    _evidence: list[EvaluationEvidence] = field(default_factory=list, repr=False)
    _errors: list[str] = field(default_factory=list, repr=False)

    @classmethod
    def create(
        cls,
        *,
        mode: EvaluationMode,
        evaluation_run_id: str | None = None,
        source_kind: str = "user_task",
        source_id: str | None = None,
        request_id: str | None = None,
        task_id: str | None = None,
        workflow_id: str | None = None,
        base_revision_id: str | None = None,
        source_revision_id: str | None = None,
    ) -> "EvaluationSession":
        source = {"kind": source_kind}
        if source_id is not None:
            source["id"] = source_id
        return cls(
            evaluation_run_id=evaluation_run_id or str(uuid4()),
            mode=EvaluationMode(mode),
            source=source,
            started_at=utc_now(),
            request_id=request_id,
            task_id=task_id,
            workflow_id=workflow_id,
            base_revision_id=base_revision_id,
            source_revision_id=source_revision_id,
        )

    def set_identity(self, **identity: str | None) -> None:
        if self.status in _TERMINAL_STATUSES:
            raise RuntimeError("cannot change identity after terminal evaluation session")
        allowed = {
            "request_id",
            "task_id",
            "workflow_id",
            "candidate_build_id",
            "candidate_revision_id",
            "change_set_id",
        }
        unknown = set(identity).difference(allowed)
        if unknown:
            raise ValueError(f"unsupported evaluation identity: {sorted(unknown)}")
        for key, value in identity.items():
            if value is not None:
                setattr(self, key, value)

    def add_evidence(self, evidence: EvaluationEvidence) -> None:
        if self.status in _TERMINAL_STATUSES:
            raise RuntimeError("cannot add evidence after terminal evaluation session")
        if any(item.evidence_id == evidence.evidence_id for item in self._evidence):
            raise ValueError(f"duplicate evaluation evidence: {evidence.evidence_id}")
        self._evidence.append(evidence)

    def emit(
        self,
        event_type: str,
        *,
        status: LiveEventStatus | str = LiveEventStatus.UNKNOWN,
        evidence_ids: tuple[str, ...] | list[str] = (),
        payload: Mapping[str, Any] | None = None,
    ) -> EvaluationEvent:
        if self.status in _TERMINAL_STATUSES:
            raise RuntimeError("cannot emit event after terminal evaluation session")
        event = EvaluationEvent(
            event_id=str(uuid4()),
            event_type=event_type,
            occurred_at=utc_now(),
            sequence=len(self._events) + 1,
            request_id=self.request_id,
            task_id=self.task_id,
            workflow_id=self.workflow_id,
            base_revision_id=self.base_revision_id,
            source_revision_id=self.source_revision_id,
            candidate_build_id=self.candidate_build_id,
            candidate_revision_id=self.candidate_revision_id,
            change_set_id=self.change_set_id,
            status=status,
            evidence_ids=tuple(evidence_ids),
            payload=dict(payload or {}),
        )
        self._events.append(event)
        return event

    def record_error(self, message: str) -> None:
        if message.strip():
            self._errors.append(message.strip()[:1000])

    def finish(self, status: EvaluationStatus | str = EvaluationStatus.FINISHED) -> None:
        if self.status in _TERMINAL_STATUSES:
            raise RuntimeError("evaluation session is already terminal")
        self.status = EvaluationStatus(status)
        self.finished_at = utc_now()

    def to_report(self) -> dict[str, Any]:
        return EvaluationReport(
            evaluation_run_id=self.evaluation_run_id,
            mode=self.mode,
            source=self.source,
            status=self.status,
            started_at=self.started_at,
            finished_at=self.finished_at,
            timeline=tuple(self._events),
            evidence=tuple(self._evidence),
            errors=tuple(self._errors),
            request_id=self.request_id,
            task_id=self.task_id,
            workflow_id=self.workflow_id,
            base_revision_id=self.base_revision_id,
            source_revision_id=self.source_revision_id,
            candidate_build_id=self.candidate_build_id,
            candidate_revision_id=self.candidate_revision_id,
            change_set_id=self.change_set_id,
        ).to_dict()
