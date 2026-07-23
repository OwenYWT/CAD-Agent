"""Request-scoped artifact storage for capability executions.

The store deliberately exposes no general-purpose absolute-path writer.  Every
generated file lives below ``<root>/<request_id>`` and every returned artifact
is content-addressed with SHA-256 metadata.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ArtifactPathError(ValueError):
    """Raised when a caller attempts to escape an allowed path root."""


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return a streaming SHA-256 digest for *path*."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactStore:
    """Confines inputs to a workspace and outputs to request directories."""

    def __init__(self, root: str | Path, *, workspace_root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.workspace_root = Path(workspace_root or self.root.parent).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate_request_id(request_id: object) -> str:
        value = str(request_id or "").strip()
        if not _REQUEST_ID_RE.fullmatch(value) or value in {".", ".."}:
            raise ArtifactPathError("request_id must contain only letters, digits, '.', '_' or '-'")
        return value

    def request_dir(self, request_id: object, *, create: bool = True) -> Path:
        safe_id = self.validate_request_id(request_id)
        directory = (self.root / safe_id).resolve()
        if not _is_within(directory, self.root):  # defense in depth
            raise ArtifactPathError("request artifact directory escapes the artifact root")
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    def output_path(
        self,
        request_id: object,
        relative_path: object,
        *,
        suffixes: Iterable[str] | None = None,
        create_parent: bool = True,
    ) -> Path:
        """Resolve a relative output path below a request directory."""

        raw = str(relative_path or "").strip()
        candidate_text = raw.replace("\\", "/")
        candidate = Path(candidate_text)
        if (
            not raw
            or "\x00" in raw
            or candidate.is_absolute()
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise ArtifactPathError("artifact output must be a safe relative path")
        directory = self.request_dir(request_id)
        resolved = (directory / candidate).resolve()
        if not _is_within(resolved, directory):
            raise ArtifactPathError("artifact output escapes its request directory")
        if suffixes is not None:
            allowed = {str(value).lower() for value in suffixes}
            if resolved.suffix.lower() not in allowed:
                raise ArtifactPathError(f"artifact output must end with one of: {', '.join(sorted(allowed))}")
        if create_parent:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def workspace_path(
        self,
        raw_path: object,
        *,
        must_exist: bool = True,
        file_only: bool = True,
        suffixes: Iterable[str] | None = None,
    ) -> Path:
        """Resolve an input confined to the configured workspace/artifact root."""

        value = str(raw_path or "").strip()
        if not value or "\x00" in value:
            raise ArtifactPathError("input path must be non-empty")
        supplied = Path(value).expanduser()
        resolved = (supplied if supplied.is_absolute() else self.workspace_root / supplied).resolve()
        if not (_is_within(resolved, self.workspace_root) or _is_within(resolved, self.root)):
            raise ArtifactPathError("input path is outside the configured workspace")
        if must_exist and not resolved.exists():
            raise ArtifactPathError(f"input path does not exist: {value}")
        if must_exist and file_only and not resolved.is_file():
            raise ArtifactPathError(f"input path is not a file: {value}")
        if suffixes is not None:
            allowed = {str(item).lower() for item in suffixes}
            lower_name = resolved.name.lower()
            if not any(lower_name.endswith(item) for item in allowed):
                raise ArtifactPathError(f"input path must end with one of: {', '.join(sorted(allowed))}")
        return resolved

    def metadata(self, path: str | Path, *, request_id: object | None = None) -> dict[str, Any]:
        resolved = Path(path).resolve()
        allowed_root = self.root
        if request_id is not None:
            allowed_root = self.request_dir(request_id, create=False)
        if not _is_within(resolved, allowed_root):
            raise ArtifactPathError("artifact metadata path is outside the allowed root")
        if not resolved.is_file():
            raise ArtifactPathError(f"artifact does not exist or is not a file: {resolved.name}")
        stat = resolved.stat()
        media_type, _ = mimetypes.guess_type(resolved.name)
        return {
            "name": resolved.name,
            "path": str(resolved),
            "relative_path": resolved.relative_to(self.root).as_posix(),
            "size_bytes": stat.st_size,
            "sha256": sha256_file(resolved),
            "media_type": media_type or "application/octet-stream",
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        }

    def list_request(self, request_id: object) -> list[dict[str, Any]]:
        directory = self.request_dir(request_id, create=False)
        if not directory.exists():
            return []
        return [
            self.metadata(path, request_id=request_id)
            for path in sorted(directory.rglob("*"))
            if path.is_file() and not path.is_symlink()
        ]

    def write_bytes(
        self,
        request_id: object,
        relative_path: object,
        data: bytes,
        *,
        suffixes: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        target = self.output_path(request_id, relative_path, suffixes=suffixes)
        # Exclusive creation prevents accidental cross-request overwrite races.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(target, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return self.metadata(target, request_id=request_id)
