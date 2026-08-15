"""Immutable PostgreSQL/S3 persistence for CAD capability inputs and outputs."""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from uuid import uuid5

from app.db import tenant_transaction
from app.domain.identity import IDENTITY_NAMESPACE
from app.object_store import get_object, put_file, sha256_object
from app.principal_context import current_principal
from sqlalchemy import text


async def commit(
    *,
    scope: str,
    request_id: str,
    filename: str,
    path: Path,
    content_type: str | None = None,
) -> dict:
    if scope not in {"uploads", "runs"}:
        raise ValueError("invalid capability artifact scope")
    context = current_principal()
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    sha256 = digest.hexdigest()
    object_key = (
        f"capabilities/tenants/{context.tenant_id}/{scope}/"
        f"{request_id}/{sha256}/{filename}"
    )
    artifact_id = uuid5(
        IDENTITY_NAMESPACE,
        f"capability-artifact:{context.tenant_id}:{scope}:"
        f"{request_id}:{filename}:{sha256}",
    )
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        existing = (
            await connection.execute(
                text(
                    """
                    SELECT id, size_bytes, sha256, object_key, content_type
                    FROM capability_artifacts
                    WHERE tenant_id=:tenant AND scope=:scope
                      AND request_id=:request AND filename=:filename
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "scope": scope,
                    "request": request_id,
                    "filename": filename,
                },
            )
        ).mappings().one_or_none()
    if existing is not None:
        if (
            int(existing["size_bytes"]) != size
            or existing["sha256"] != sha256
            or existing["object_key"] != object_key
        ):
            raise ValueError(
                "capability request filename already has immutable content"
            )
        stored = await sha256_object(object_key)
        if stored["size_bytes"] != size or stored["sha256"] != sha256:
            raise RuntimeError("capability artifact checksum mismatch")
        return {
            **dict(existing),
            "size_bytes": size,
            "filename": filename,
            "scope": scope,
            "request_id": request_id,
        }
    media_type = (
        content_type
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    uploaded = await put_file(object_key, path, content_type=media_type)
    if uploaded["size_bytes"] != size or uploaded["sha256"] != sha256:
        raise RuntimeError("capability artifact upload verification failed")
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await connection.execute(
            text(
                """
                INSERT INTO capability_artifacts (
                    id, tenant_id, principal_id, scope, request_id,
                    filename, content_type, size_bytes, sha256, object_key
                ) VALUES (
                    :id, :tenant, :principal, :scope, :request,
                    :filename, :content_type, :size, :sha256, :object_key
                )
                """
            ),
            {
                "id": artifact_id,
                "tenant": context.tenant_id,
                "principal": context.principal_id,
                "scope": scope,
                "request": request_id,
                "filename": filename,
                "content_type": media_type,
                "size": size,
                "sha256": sha256,
                "object_key": object_key,
            },
        )
    return {
        "id": artifact_id,
        "scope": scope,
        "request_id": request_id,
        "filename": filename,
        "content_type": media_type,
        "size_bytes": size,
        "sha256": sha256,
        "object_key": object_key,
    }


async def get(scope: str, request_id: str, filename: str) -> dict | None:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT id, scope, request_id, filename, content_type,
                           size_bytes, sha256, object_key
                    FROM capability_artifacts
                    WHERE tenant_id=:tenant AND scope=:scope
                      AND request_id=:request AND filename=:filename
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "scope": scope,
                    "request": request_id,
                    "filename": filename,
                },
            )
        ).mappings().one_or_none()
    return dict(row) if row else None


async def hydrate_reference(reference: str, owner_root: Path) -> None:
    parts = Path(reference).parts
    if len(parts) != 3 or parts[0] not in {"uploads", "runs"}:
        return
    scope, request_id, filename = parts
    metadata = await get(scope, request_id, filename)
    if metadata is None:
        return
    target = (owner_root / scope / request_id / filename).resolve()
    if not target.is_relative_to(owner_root.resolve()):
        raise ValueError("capability artifact path escaped owner workspace")
    if target.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = await get_object(metadata["object_key"])
    if (
        len(payload) != metadata["size_bytes"]
        or hashlib.sha256(payload).hexdigest() != metadata["sha256"]
    ):
        raise RuntimeError("capability artifact download verification failed")
    target.write_bytes(payload)


async def hydrate_values(value, owner_root: Path) -> None:
    if isinstance(value, str):
        await hydrate_reference(value, owner_root)
    elif isinstance(value, dict):
        for item in value.values():
            await hydrate_values(item, owner_root)
    elif isinstance(value, list):
        for item in value:
            await hydrate_values(item, owner_root)
