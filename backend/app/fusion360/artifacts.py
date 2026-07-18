"""Confinement and verification for files produced by Fusion Desktop."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path

from .contract import CadArtifact, LocalArtifact
from .errors import FusionConnectorError

MAX_ARTIFACT_FILE_BYTES = 512 * 1024 * 1024
MAX_ARTIFACT_OWNER_BYTES = 2 * 1024 * 1024 * 1024
_BASENAME = re.compile(r"^[^/\\\x00-\x1f]{1,255}$")

MEDIA_TYPES = {
    "step": "model/step",
    "stl": "model/stl",
    "dxf": "image/vnd.dxf",
    "f3d": "application/vnd.autodesk.fusion360",
    "png": "image/png",
}


def safe_basename(filename: str) -> str:
    if filename in {".", ".."} or not _BASENAME.fullmatch(filename):
        raise FusionConnectorError("PATH_NOT_ALLOWED", "Artifact filename must be a basename")
    return filename


def root_fingerprint(root: Path) -> str:
    canonical = str(root.expanduser().resolve()).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class ArtifactStore:
    root: Path
    max_file_bytes: int = MAX_ARTIFACT_FILE_BYTES
    max_owner_bytes: int = MAX_ARTIFACT_OWNER_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.expanduser().resolve())

    @property
    def fingerprint(self) -> str:
        return root_fingerprint(self.root)

    def request_dir(self, owner_id: str, request_id: str, *, create: bool = False) -> Path:
        owner_hash = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:24]
        try:
            request_name = str(uuid.UUID(str(request_id)))
        except ValueError as exc:
            raise FusionConnectorError("PATH_NOT_ALLOWED", "Invalid artifact request ID") from exc
        candidate = (self.root / owner_hash / request_name).resolve()
        if not candidate.is_relative_to(self.root):
            raise FusionConnectorError("PATH_NOT_ALLOWED", "Artifact directory escapes configured root")
        if create:
            candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        return candidate

    def staging_path(self, owner_id: str, request_id: str, filename: str) -> Path:
        return self.request_dir(owner_id, request_id, create=True) / safe_basename(filename)

    def resolve_local(self, owner_id: str, request_id: str, relative_path: str) -> Path:
        base = self.request_dir(owner_id, request_id)
        candidate = (base / relative_path).resolve()
        if not candidate.is_relative_to(base):
            raise FusionConnectorError("PATH_NOT_ALLOWED", "Artifact path escapes request directory")
        return candidate

    def _owner_usage(self, owner_id: str) -> int:
        owner_dir = self.request_dir(owner_id, str(uuid.uuid4())).parent
        if not owner_dir.exists():
            return 0
        return sum(p.stat().st_size for p in owner_dir.rglob("*") if p.is_file())

    def validate_file(self, path: Path, kind: str) -> tuple[int, str]:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise FusionConnectorError("ARTIFACT_INVALID", "Artifact file is missing or outside the configured root")
        size = resolved.stat().st_size
        if size <= 0:
            raise FusionConnectorError("ARTIFACT_INVALID", "Artifact file is empty")
        if size > self.max_file_bytes:
            raise FusionConnectorError("ARTIFACT_QUOTA_EXCEEDED", "Artifact exceeds the per-file limit")
        with resolved.open("rb") as handle:
            head = handle.read(512)
            digest = hashlib.sha256()
            digest.update(head)
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        if not _has_signature(kind, head, size):
            raise FusionConnectorError("ARTIFACT_INVALID", f"Artifact does not have a valid {kind.upper()} signature")
        return size, digest.hexdigest()

    def accept_local(self, owner_id: str, request_id: str, artifact: LocalArtifact) -> CadArtifact:
        if self._owner_usage(owner_id) > self.max_owner_bytes:
            raise FusionConnectorError("ARTIFACT_QUOTA_EXCEEDED", "Owner artifact quota exceeded")
        path = self.resolve_local(owner_id, request_id, artifact.relative_path)
        size, sha256 = self.validate_file(path, artifact.kind)
        if size != artifact.size_bytes or sha256 != artifact.sha256:
            raise FusionConnectorError("ARTIFACT_INVALID", "Artifact metadata does not match the produced file")
        filename = safe_basename(artifact.filename)
        media_type = MEDIA_TYPES.get(artifact.kind, mimetypes.guess_type(filename)[0] or "application/octet-stream")
        return CadArtifact(
            artifact_id=artifact.artifact_id,
            kind=artifact.kind,
            filename=filename,
            media_type=media_type,
            size_bytes=size,
            sha256=sha256,
            download_url=f"/api/cad/fusion360/artifacts/{request_id}/{filename}",
        )


def _has_signature(kind: str, head: bytes, size: int) -> bool:
    lower = head.lower()
    if kind == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if kind == "step":
        return b"ISO-10303-21" in head.upper()
    if kind == "dxf":
        return b"SECTION" in head.upper() and (b"HEADER" in head.upper() or b"ENTITIES" in head.upper())
    if kind == "stl":
        if lower.lstrip().startswith(b"solid") and b"facet" in lower:
            return True
        if len(head) < 84:
            return False
        triangle_count = struct.unpack("<I", head[80:84])[0]
        return triangle_count > 0 and size == 84 + triangle_count * 50
    if kind == "f3d":
        return head.startswith((b"PK\x03\x04", b"Fusion 360"))
    return False


def atomic_replace_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Durably replace a local metadata file without exposing a partial write."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()
