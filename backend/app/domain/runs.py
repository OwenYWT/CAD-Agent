"""Durable workflow, step, attempt, lease, and event contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class StepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class AttemptStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


WORKFLOW_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.PENDING: frozenset(
        {WorkflowStatus.PLANNING, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.PLANNING: frozenset(
        {
            WorkflowStatus.RUNNING,
            WorkflowStatus.WAITING_CONFIRMATION,
            WorkflowStatus.CANCELLING,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
            WorkflowStatus.TIMED_OUT,
        }
    ),
    WorkflowStatus.RUNNING: frozenset(
        {
            WorkflowStatus.WAITING_CONFIRMATION,
            WorkflowStatus.CANCELLING,
            WorkflowStatus.SUCCEEDED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
            WorkflowStatus.TIMED_OUT,
        }
    ),
    WorkflowStatus.WAITING_CONFIRMATION: frozenset(
        {
            WorkflowStatus.RUNNING,
            WorkflowStatus.CANCELLING,
            WorkflowStatus.CANCELLED,
            WorkflowStatus.TIMED_OUT,
        }
    ),
    WorkflowStatus.CANCELLING: frozenset(
        {
            WorkflowStatus.CANCELLED,
            WorkflowStatus.FAILED,
            WorkflowStatus.TIMED_OUT,
        }
    ),
    WorkflowStatus.SUCCEEDED: frozenset(),
    WorkflowStatus.FAILED: frozenset(),
    WorkflowStatus.CANCELLED: frozenset(),
    WorkflowStatus.TIMED_OUT: frozenset(),
}

STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset(
        {StepStatus.READY, StepStatus.SKIPPED, StepStatus.CANCELLED}
    ),
    StepStatus.READY: frozenset(
        {StepStatus.RUNNING, StepStatus.CANCELLED, StepStatus.TIMED_OUT}
    ),
    StepStatus.RUNNING: frozenset(
        {
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.CANCELLED,
            StepStatus.TIMED_OUT,
        }
    ),
    StepStatus.SUCCEEDED: frozenset(),
    StepStatus.FAILED: frozenset({StepStatus.READY, StepStatus.CANCELLED}),
    StepStatus.SKIPPED: frozenset(),
    StepStatus.CANCELLED: frozenset(),
    StepStatus.TIMED_OUT: frozenset({StepStatus.READY}),
}

ATTEMPT_TRANSITIONS: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.PENDING: frozenset(
        {
            AttemptStatus.LEASED,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELLED,
            AttemptStatus.TIMED_OUT,
        }
    ),
    AttemptStatus.LEASED: frozenset(
        {
            AttemptStatus.RUNNING,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELLED,
            AttemptStatus.TIMED_OUT,
        }
    ),
    AttemptStatus.RUNNING: frozenset(
        {
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELLED,
            AttemptStatus.TIMED_OUT,
        }
    ),
    AttemptStatus.SUCCEEDED: frozenset(),
    AttemptStatus.FAILED: frozenset(),
    AttemptStatus.CANCELLED: frozenset(),
    AttemptStatus.TIMED_OUT: frozenset(),
}


@dataclass(frozen=True, slots=True)
class WorkflowCreated:
    workflow_id: UUID
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class StepCreated:
    step_id: UUID
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class AttemptCreated:
    attempt_id: UUID
    attempt_number: int
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class AttemptLease:
    attempt_id: UUID
    generation: int
    leased_until_iso: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AttemptCompletion:
    attempt_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class EventAppended:
    event_id: UUID
    outbox_id: UUID
    sequence: int
