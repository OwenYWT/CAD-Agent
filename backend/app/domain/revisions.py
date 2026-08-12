"""Immutable revision and candidate Change Set contracts."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
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


class CandidateBuildStatus(StrEnum):
    BUILDING = "building"
    REVIEWABLE = "reviewable"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class AgentCandidateBuildCreated:
    candidate_build_id: UUID
    status: CandidateBuildStatus
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class AgentCandidateBuildCompleted:
    candidate_build_id: UUID
    status: CandidateBuildStatus
    replayed: bool = False
