"""Two-phase immutable artifact contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ArtifactUploadAuthorization:
    upload_id: UUID
    staging_object_key: str
    expires_at_iso: str
    required_headers: dict[str, str]
    upload_url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CommittedArtifact:
    artifact_id: UUID
    upload_id: UUID
    filename: str
    artifact_kind: str
    object_key: str
    size_bytes: int
    sha256: str
    content_type: str


@dataclass(frozen=True, slots=True)
class ArtifactCommitResult:
    attempt_id: UUID
    revision_id: UUID
    artifacts: tuple[CommittedArtifact, ...]
    replayed: bool = False
