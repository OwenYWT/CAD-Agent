"""Immutable revision and candidate Change Set contracts."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class InitialBranchCreated:
    branch_id: UUID
    revision_id: UUID
    revision_number: int
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class CandidateChangeSetCreated:
    change_set_id: UUID
    candidate_revision_id: UUID
    revision_number: int
    replayed: bool = False
