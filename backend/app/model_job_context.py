"""Fencing identity for writes made by a durable model job."""
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ModelJobContext:
    job_id: UUID
    generation: int


model_job_context: ContextVar[ModelJobContext | None] = ContextVar('model_job_context', default=None)
