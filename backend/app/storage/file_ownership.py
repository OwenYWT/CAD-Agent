"""Request-scoped ownership metadata for legacy generated CAD artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
from pathlib import Path
from typing import Awaitable, Generic, TypeVar
from uuid import UUID, uuid5

from app.config import settings
from app.domain.identity import IDENTITY_NAMESPACE
from app.execution.canonical import sha256_file


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_OWNER_FILE = ".owner"
_CACHE_MARKER = ".materialized"


class FileOwnershipError(RuntimeError):
    pass


_T = TypeVar("_T")


class _ImmediateAwaitable(Awaitable[_T], Generic[_T]):
    """A completed awaitable used to preserve the legacy synchronous contract."""

    def __init__(self, value: _T):
        self._value = value

    def __await__(self):
        if False:
            yield None
        return self._value


def _request_dir(request_id: str) -> Path:
    if not _SAFE_ID.fullmatch(request_id):
        raise FileOwnershipError("Invalid request ID")
    root = Path(settings.file_storage_dir).expanduser().resolve()
    target = (root / request_id).resolve()
    if target.parent != root:
        raise FileOwnershipError("Invalid request path")
    return target


def expired_request_files(directory: Path, cutoff: float) -> list[Path]:
    """Only completed, flat request caches belong to the TTL collector."""
    if directory.is_symlink() or not directory.is_dir():
        return []
    if not any((directory / name).is_file() and not (directory / name).is_symlink()
               for name in (_OWNER_FILE, _CACHE_MARKER)):
        return []
    entries = list(directory.iterdir())
    if directory.stat().st_mtime >= cutoff or any(
        path.is_symlink() or not path.is_file() or path.stat().st_mtime >= cutoff for path in entries
    ):
        return []
    return entries


def _fingerprint(principal: str) -> str:
    return hashlib.sha256(principal.encode("utf-8")).hexdigest()


def claim_request_owner(
    request_id: str | None,
    principal: str | None,
    *,
    revision_id: str | None = None,
) -> Awaitable[None]:
    """Bind an existing request directory to a login user or legacy API key."""
    if not request_id:
        return _ImmediateAwaitable(None)
    if not settings.durable_control_plane_enabled and (not isinstance(principal, str) or not principal):
        if not settings.auth_required and not settings.api_keys:
            directory = _request_dir(request_id)
            if directory.is_dir():
                (directory / _CACHE_MARKER).touch()
        return _ImmediateAwaitable(None)
    if settings.durable_control_plane_enabled:
        return _commit_request_files(request_id, revision_id=revision_id)
    request_dir = _request_dir(request_id)
    if not request_dir.is_dir():
        return _ImmediateAwaitable(None)
    marker = request_dir / _OWNER_FILE
    expected = _fingerprint(principal)
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        actual = marker.read_text(encoding="ascii").strip()
        if not hmac.compare_digest(actual, expected):
            raise FileOwnershipError("Request artifacts already belong to another user")
        return _ImmediateAwaitable(None)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(expected)
    return _ImmediateAwaitable(None)


async def request_belongs_to(request_id: str, principal: str | None) -> bool:
    """Auth-off development remains compatible; authenticated mode is fail-closed."""
    if settings.durable_control_plane_enabled:
        from app.db import tenant_transaction
        from app.principal_context import current_principal
        from sqlalchemy import text

        context = current_principal()
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            return bool(
                await connection.scalar(
                    text(
                        """
                        SELECT 1
                        FROM (
                            SELECT project_id
                            FROM project_files
                            WHERE tenant_id=:tenant
                              AND request_id=:request
                            UNION ALL
                            SELECT project_id
                            FROM artifacts
                            WHERE tenant_id=:tenant
                              AND workflow_run_id::text=:request
                        ) owned
                        JOIN project_memberships membership
                          ON membership.tenant_id=:tenant
                         AND membership.project_id=owned.project_id
                         AND membership.principal_id=:principal
                        LIMIT 1
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "request": request_id,
                        "principal": context.principal_id,
                    },
                )
            )
    if not isinstance(principal, str):
        principal = None
    if principal is None:
        return not settings.auth_required and not settings.api_keys
    marker = _request_dir(request_id) / _OWNER_FILE
    try:
        actual = marker.read_text(encoding="ascii").strip()
    except OSError:
        return False
    return hmac.compare_digest(actual, _fingerprint(principal))


async def get_project_file(request_id: str, filename: str) -> dict | None:
    """Return tenant-scoped immutable file metadata for downloads/analysis."""
    if not settings.durable_control_plane_enabled:
        return None
    from app.db import tenant_transaction
    from app.principal_context import current_principal
    from sqlalchemy import text

    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT id, project_id, revision_id, request_id, filename,
                           content_type, size_bytes, sha256, object_key
                    FROM project_files
                    WHERE tenant_id=:tenant AND request_id=:request
                      AND filename=:filename
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "request": request_id,
                    "filename": filename,
                },
            )
        ).mappings().one_or_none()
        if row is None:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT a.id, a.project_id, a.revision_id,
                               a.workflow_run_id::text AS request_id,
                               a.filename, a.content_type, a.size_bytes,
                               a.sha256, a.object_key
                        FROM artifacts a
                        JOIN project_memberships membership
                          ON membership.tenant_id=a.tenant_id
                         AND membership.project_id=a.project_id
                         AND membership.principal_id=:principal
                        WHERE a.tenant_id=:tenant
                          AND a.workflow_run_id::text=:request
                          AND a.filename=:filename
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "principal": context.principal_id,
                        "request": request_id,
                        "filename": filename,
                    },
                )
            ).mappings().one_or_none()
    return dict(row) if row else None


async def list_project_files(request_id: str) -> list[dict]:
    if not settings.durable_control_plane_enabled:
        return []
    from app.db import tenant_transaction
    from app.principal_context import current_principal
    from sqlalchemy import text

    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, project_id, revision_id, request_id, filename,
                           content_type, size_bytes, sha256, object_key
                    FROM project_files
                    WHERE tenant_id=:tenant AND request_id=:request
                    ORDER BY filename
                    """
                ),
                {"tenant": context.tenant_id, "request": request_id},
            )
        ).mappings().all()
        if not rows:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT a.id, a.project_id, a.revision_id,
                               a.workflow_run_id::text AS request_id,
                               a.filename, a.content_type, a.size_bytes,
                               a.sha256, a.object_key
                        FROM artifacts a
                        JOIN project_memberships membership
                          ON membership.tenant_id=a.tenant_id
                         AND membership.project_id=a.project_id
                         AND membership.principal_id=:principal
                        WHERE a.tenant_id=:tenant
                          AND a.workflow_run_id::text=:request
                        ORDER BY a.filename
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "principal": context.principal_id,
                        "request": request_id,
                    },
                )
            ).mappings().all()
    return [dict(row) for row in rows]


async def hydrate_project_files(
    request_id: str,
    *,
    extensions: set[str] | None = None,
) -> list[Path]:
    """Restore tenant-owned immutable artifacts into the disposable local cache.

    PostgreSQL/S3 remain authoritative in durable mode.  This adapter exists for
    CAD libraries and external connector clients that require filesystem paths.
    Every restored byte stream is checked against the committed size and SHA-256.
    """
    if not settings.durable_control_plane_enabled:
        request_dir = _request_dir(request_id)
        if not request_dir.is_dir():
            return []
        return [
            path
            for path in sorted(request_dir.iterdir())
            if path.is_file() and path.name not in {_OWNER_FILE, _CACHE_MARKER}
        ]

    from app.object_store import download_verified_object, ObjectIntegrityError

    normalized_extensions = (
        {suffix.lower() for suffix in extensions}
        if extensions is not None
        else None
    )
    metadata = await list_project_files(request_id)
    request_dir = _request_dir(request_id)
    request_dir.mkdir(parents=True, exist_ok=True)
    marker = request_dir / _CACHE_MARKER
    if marker.is_file():
        marker.touch()
    hydrated: list[Path] = []
    for record in metadata:
        filename = str(record["filename"])
        if not re.fullmatch(r"[A-Za-z0-9._-]+", filename):
            raise FileOwnershipError("Stored artifact filename is unsafe")
        target = (request_dir / filename).resolve()
        if target.parent != request_dir:
            raise FileOwnershipError("Stored artifact path escaped its request")
        if (
            normalized_extensions is not None
            and target.suffix.lower() not in normalized_extensions
        ):
            continue

        expected_size = int(record["size_bytes"])
        expected_sha = str(record["sha256"])
        valid_cache = (target.is_file() and not target.is_symlink()
                       and target.stat().st_size == expected_size
                       and await asyncio.to_thread(sha256_file, target) == expected_sha)
        if not valid_cache:
            try:
                await download_verified_object(record, target)
            except ObjectIntegrityError as exc:
                raise FileOwnershipError("Stored artifact checksum mismatch") from exc
        hydrated.append(target)
    if hydrated:
        (request_dir / _CACHE_MARKER).touch()
    return hydrated


async def _commit_request_files(
    request_id: str,
    *,
    revision_id: str | None,
) -> None:
    from app.db import tenant_transaction
    from app.object_store import put_file, sha256_object
    from app.principal_context import current_principal
    from app.repositories.identity import ensure_principal
    from sqlalchemy import text

    request_dir = _request_dir(request_id)
    if not request_dir.is_dir():
        return
    context = current_principal()
    resolved_revision: UUID | None = None
    project_id: UUID | None = None
    if revision_id:
        try:
            resolved_revision = UUID(revision_id)
        except ValueError as exc:
            raise FileOwnershipError("Invalid revision ID") from exc
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            project_id = await connection.scalar(
                text(
                    """
                    SELECT project_id FROM project_revisions
                    WHERE tenant_id=:tenant AND id=:revision
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "revision": resolved_revision,
                },
            )
        if project_id is None:
            raise FileOwnershipError("Revision does not belong to this tenant")
    if project_id is None:
        project_id = uuid5(
            IDENTITY_NAMESPACE,
            f"direct-artifact-project:{context.tenant_id}:{request_id}",
        )
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            await ensure_principal(connection, context)
            await connection.execute(
                text(
                    """
                    INSERT INTO projects (
                        id, tenant_id, name, slug, created_by_principal_id
                    ) VALUES (
                        :id, :tenant, '直接生成的 CAD 产物',
                        :slug, :principal
                    ) ON CONFLICT (id) DO NOTHING
                    """
                ),
                {
                    "id": project_id,
                    "tenant": context.tenant_id,
                    "slug": "direct-"
                    + hashlib.sha256(request_id.encode()).hexdigest()[:20],
                    "principal": context.principal_id,
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO project_memberships (
                        tenant_id, project_id, principal_id, role
                    ) VALUES (:tenant, :project, :principal, 'owner')
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "project": project_id,
                    "principal": context.principal_id,
                },
            )

    for path in sorted(request_dir.iterdir()):
        if (
            path.name in {_OWNER_FILE, _CACHE_MARKER}
            or path.is_symlink()
            or not path.is_file()
        ):
            continue
        filename = path.name
        if not re.fullmatch(r"[A-Za-z0-9._-]+", filename):
            raise FileOwnershipError("Generated filename is unsafe")
        size_bytes = path.stat().st_size
        sha256 = await asyncio.to_thread(sha256_file, path)

        object_key = (
            f"workspace/tenants/{context.tenant_id}/projects/{project_id}/"
            f"requests/{request_id}/{sha256}/{filename}"
        )
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            existing = (
                await connection.execute(
                    text(
                        """
                        SELECT size_bytes, sha256, object_key
                        FROM project_files
                        WHERE tenant_id=:tenant AND request_id=:request
                          AND filename=:filename
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "request": request_id,
                        "filename": filename,
                    },
                )
            ).mappings().one_or_none()
        if existing is not None:
            if (
                int(existing["size_bytes"]) != size_bytes
                or existing["sha256"] != sha256
                or existing["object_key"] != object_key
            ):
                raise FileOwnershipError(
                    "Request filename already has different immutable content"
                )
            stored = await sha256_object(object_key)
            if (
                stored["size_bytes"] != size_bytes
                or stored["sha256"] != sha256
            ):
                raise FileOwnershipError("Stored artifact checksum mismatch")
            continue
        content_type = {
            ".step": "application/step",
            ".stp": "application/step",
            ".stl": "application/sla",
            ".dxf": "application/dxf",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".py": "text/x-python",
            ".bas": "text/plain",
        }.get(path.suffix.lower(), "application/octet-stream")
        uploaded = await put_file(
            object_key,
            path,
            content_type=content_type,
        )
        if (
            uploaded["size_bytes"] != size_bytes
            or uploaded["sha256"] != sha256
        ):
            raise FileOwnershipError("Artifact upload verification failed")
        file_id = uuid5(
            IDENTITY_NAMESPACE,
            f"project-file:{context.tenant_id}:{request_id}:{filename}:{sha256}",
        )
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO project_files (
                        id, tenant_id, project_id, revision_id, request_id,
                        filename, content_type, size_bytes, sha256,
                        object_key, source
                    ) VALUES (
                        :id, :tenant, :project, :revision, :request,
                        :filename, :content_type, :size, :sha256,
                        :object_key, 'workspace'
                    )
                    ON CONFLICT (
                        tenant_id, request_id, filename
                    ) DO NOTHING
                    """
                ),
                {
                    "id": file_id,
                    "tenant": context.tenant_id,
                    "project": project_id,
                    "revision": resolved_revision,
                    "request": request_id,
                    "filename": filename,
                    "content_type": content_type,
                    "size": size_bytes,
                    "sha256": sha256,
                    "object_key": object_key,
                },
            )
    (request_dir / _CACHE_MARKER).touch()
