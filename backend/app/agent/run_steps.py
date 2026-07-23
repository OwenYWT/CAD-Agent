from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from app.models.schemas import StepUpdate


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _stage_id(step: str, attempt: int | None) -> str:
    return f"{step}:{attempt}" if attempt is not None else step




def _derive_status(step: str, status: str) -> str:
    if status != "running":
        return status
    if step == "complete":
        return "success"
    if step == "failed":
        return "failed"
    if step in {"fixing_error", "repairing_code"}:
        return "warn"
    return status


def ensure_timeline_fields(step: StepUpdate) -> StepUpdate:
    if step.stage_id and step.started_at and step.status != "running":
        return step
    return StepUpdate(
        step=step.step,
        message=step.message,
        status=_derive_status(step.step, step.status),
        stage_id=step.stage_id or _stage_id(step.step, step.attempt),
        attempt=step.attempt,
        started_at=step.started_at or _utc_now_iso(),
        duration_ms=step.duration_ms,
        detail=step.detail,
        part_name=step.part_name,
        part_index=step.part_index,
        total_parts=step.total_parts,
    )

def make_step(
    step: str,
    message: str,
    *,
    status: str = "running",
    attempt: int | None = None,
    started_at: str | None = None,
    duration_ms: int | None = None,
    detail: dict[str, Any] | None = None,
    part_name: str | None = None,
    part_index: int | None = None,
    total_parts: int | None = None,
) -> StepUpdate:
    return StepUpdate(
        step=step,
        message=message,
        status=status,
        stage_id=_stage_id(step, attempt),
        attempt=attempt,
        started_at=started_at or _utc_now_iso(),
        duration_ms=duration_ms,
        detail=detail,
        part_name=part_name,
        part_index=part_index,
        total_parts=total_parts,
    )


@dataclass
class RunStageTimer:
    step: str
    message: str
    attempt: int | None = None
    detail: dict[str, Any] | None = None
    part_name: str | None = None
    part_index: int | None = None
    total_parts: int | None = None
    _started_at: str | None = field(default=None, init=False)
    _start_perf: float | None = field(default=None, init=False)

    def start(self, *, status: str = "running") -> StepUpdate:
        self._started_at = _utc_now_iso()
        self._start_perf = perf_counter()
        return make_step(
            self.step,
            self.message,
            status=status,
            attempt=self.attempt,
            started_at=self._started_at,
            detail=self.detail,
            part_name=self.part_name,
            part_index=self.part_index,
            total_parts=self.total_parts,
        )

    def complete(
        self,
        message: str | None = None,
        *,
        status: str = "success",
        detail: dict[str, Any] | None = None,
    ) -> StepUpdate:
        duration_ms = None
        if self._start_perf is not None:
            duration_ms = int((perf_counter() - self._start_perf) * 1000)
        return make_step(
            self.step,
            message or self.message,
            status=status,
            attempt=self.attempt,
            started_at=self._started_at,
            duration_ms=duration_ms,
            detail=detail if detail is not None else self.detail,
            part_name=self.part_name,
            part_index=self.part_index,
            total_parts=self.total_parts,
        )
