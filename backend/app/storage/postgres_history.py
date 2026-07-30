"""PostgreSQL implementation of sessions, revisions, feedback, and Onshape links."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.identity import (
    IDENTITY_NAMESPACE,
    PrincipalContext,
    user_principal,
)
from app.execution.canonical import canonical_sha256
from app.principal_context import current_principal
from app.repositories.identity import ensure_principal


def _context(user_id: str | None = None) -> PrincipalContext:
    return user_principal(user_id) if user_id else current_principal()


def _iso(value: datetime | str | None) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _stable_uuid(kind: str, context: PrincipalContext, value: str) -> UUID:
    return uuid5(
        IDENTITY_NAMESPACE,
        f"workspace:{kind}:{context.tenant_id}:{value}",
    )


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _session(row: Any) -> dict:
    return {
        "id": row["id"],
        "user_id": row.get("legacy_user_id"),
        "title": row["title"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


async def create_session(
    session_id: str,
    title: str = "",
    user_id: str | None = None,
) -> dict:
    context = _context(user_id)
    project_id = _stable_uuid("project", context, session_id)
    slug = f"workspace-{hashlib.sha256(session_id.encode()).hexdigest()[:20]}"
    now = datetime.now(timezone.utc)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await ensure_principal(connection, context)
        await connection.execute(
            text(
                """
                INSERT INTO projects (
                    id, tenant_id, name, slug, created_by_principal_id,
                    created_at, updated_at
                ) VALUES (
                    :id, :tenant, :name, :slug, :principal, :now, :now
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": project_id,
                "tenant": context.tenant_id,
                "name": title.strip() or "未命名 CAD 项目",
                "slug": slug,
                "principal": context.principal_id,
                "now": now,
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
        await connection.execute(
            text(
                """
                INSERT INTO workspace_sessions (
                    id, tenant_id, project_id, created_by_principal_id,
                    legacy_user_id, title, created_at, updated_at
                ) VALUES (
                    :id, :tenant, :project, :principal,
                    :user_id, :title, :now, :now
                )
                ON CONFLICT (tenant_id, id) DO UPDATE
                SET legacy_user_id=COALESCE(
                        workspace_sessions.legacy_user_id,
                        EXCLUDED.legacy_user_id
                    ),
                    title=CASE
                        WHEN BTRIM(workspace_sessions.title)='' AND
                             BTRIM(EXCLUDED.title)<>''
                        THEN EXCLUDED.title
                        ELSE workspace_sessions.title
                    END,
                    updated_at=CURRENT_TIMESTAMP
                """
            ),
            {
                "id": session_id,
                "tenant": context.tenant_id,
                "project": project_id,
                "principal": context.principal_id,
                "user_id": user_id,
                "title": title,
                "now": now,
            },
        )
        row = (
            await connection.execute(
                text(
                    """
                    SELECT id, legacy_user_id, title, created_at, updated_at
                    FROM workspace_sessions
                    WHERE tenant_id=:tenant AND id=:id
                    """
                ),
                {"tenant": context.tenant_id, "id": session_id},
            )
        ).mappings().one()
    return _session(row)


async def list_sessions(user_id: str | None = None) -> list[dict]:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, legacy_user_id, title, created_at, updated_at
                    FROM workspace_sessions
                    ORDER BY updated_at DESC LIMIT 50
                    """
                )
            )
        ).mappings().all()
    return [_session(row) for row in rows]


async def session_belongs_to_user(
    session_id: str,
    user_id: str | None,
) -> bool:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        return bool(
            await connection.scalar(
                text(
                    "SELECT 1 FROM workspace_sessions "
                    "WHERE tenant_id=:tenant AND id=:id"
                ),
                {"tenant": context.tenant_id, "id": session_id},
            )
        )


async def session_writable_by_user(
    session_id: str,
    user_id: str | None,
) -> bool:
    # Tenant RLS makes another customer's identically named session invisible.
    return await session_belongs_to_user(session_id, user_id) or not await _session_exists(
        session_id,
        _context(user_id),
    )


async def _session_exists(
    session_id: str,
    context: PrincipalContext,
) -> bool:
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        return bool(
            await connection.scalar(
                text(
                    "SELECT 1 FROM workspace_sessions "
                    "WHERE tenant_id=:tenant AND id=:id"
                ),
                {"tenant": context.tenant_id, "id": session_id},
            )
        )


async def panel_writable_by_session(
    panel_id: str,
    session_id: str,
    user_id: str | None,
) -> bool:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        existing = await connection.scalar(
            text(
                "SELECT session_id FROM workspace_panels "
                "WHERE tenant_id=:tenant AND id=:id"
            ),
            {"tenant": context.tenant_id, "id": panel_id},
        )
    return existing is None or existing == session_id


async def panel_belongs_to_user(
    panel_id: str,
    user_id: str | None,
) -> bool:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        return bool(
            await connection.scalar(
                text(
                    "SELECT 1 FROM workspace_panels "
                    "WHERE tenant_id=:tenant AND id=:id"
                ),
                {"tenant": context.tenant_id, "id": panel_id},
            )
        )


async def delete_session(
    session_id: str,
    user_id: str | None = None,
) -> None:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        project_id = await connection.scalar(
            text(
                "SELECT project_id FROM workspace_sessions "
                "WHERE tenant_id=:tenant AND id=:id"
            ),
            {"tenant": context.tenant_id, "id": session_id},
        )
        if project_id:
            await connection.execute(
                text(
                    "DELETE FROM workspace_sessions "
                    "WHERE tenant_id=:tenant AND id=:session"
                ),
                {"tenant": context.tenant_id, "session": session_id},
            )
            await connection.execute(
                text(
                    """
                    UPDATE projects SET status='deleted',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=:tenant AND id=:project
                    """
                ),
                {"tenant": context.tenant_id, "project": project_id},
            )


async def touch_session(session_id: str) -> None:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await connection.execute(
            text(
                """
                UPDATE workspace_sessions
                SET updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=:tenant AND id=:id
                """
            ),
            {"tenant": context.tenant_id, "id": session_id},
        )


async def create_panel(
    session_id: str,
    panel_id: str,
    title: str = "",
    user_id: str | None = None,
) -> dict:
    context = _context(user_id)
    await create_session(session_id, user_id=user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        existing = await connection.scalar(
            text(
                "SELECT session_id FROM workspace_panels "
                "WHERE tenant_id=:tenant AND id=:id"
            ),
            {"tenant": context.tenant_id, "id": panel_id},
        )
        if existing is not None and existing != session_id:
            raise ValueError("panel_id already belongs to another session")
        await connection.execute(
            text(
                """
                INSERT INTO workspace_panels (
                    id, tenant_id, session_id, title
                ) VALUES (:id, :tenant, :session, :title)
                ON CONFLICT (tenant_id, id) DO NOTHING
                """
            ),
            {
                "id": panel_id,
                "tenant": context.tenant_id,
                "session": session_id,
                "title": title,
            },
        )
        row = (
            await connection.execute(
                text(
                    """
                    SELECT id, session_id, title, created_at
                    FROM workspace_panels
                    WHERE tenant_id=:tenant AND id=:id
                    """
                ),
                {"tenant": context.tenant_id, "id": panel_id},
            )
        ).mappings().one()
    return {**dict(row), "created_at": _iso(row["created_at"])}


async def list_panels(session_id: str) -> list[dict]:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, session_id, title, current_code, created_at
                    FROM workspace_panels
                    WHERE tenant_id=:tenant AND session_id=:session
                    ORDER BY created_at
                    """
                ),
                {"tenant": context.tenant_id, "session": session_id},
            )
        ).mappings().all()
    return [
        {**dict(row), "created_at": _iso(row["created_at"])}
        for row in rows
    ]


async def update_panel_code(
    panel_id: str,
    code: str | None,
    params: dict | None = None,
) -> None:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await connection.execute(
            text(
                """
                UPDATE workspace_panels
                SET current_code=:code, current_params=CAST(:params AS jsonb)
                WHERE tenant_id=:tenant AND id=:id
                """
            ),
            {
                "code": code,
                "params": _json(params) if params is not None else None,
                "tenant": context.tenant_id,
                "id": panel_id,
            },
        )


async def save_message(
    panel_id: str,
    role: str,
    content: str,
    result: dict | None = None,
) -> None:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await connection.execute(
            text(
                """
                INSERT INTO workspace_messages (
                    tenant_id, panel_id, role, content, result
                ) VALUES (
                    :tenant, :panel, :role, :content, CAST(:result AS jsonb)
                )
                """
            ),
            {
                "tenant": context.tenant_id,
                "panel": panel_id,
                "role": role,
                "content": content,
                "result": _json(result) if result is not None else None,
            },
        )


async def get_messages(panel_id: str) -> list[dict]:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT role, content, result FROM workspace_messages
                    WHERE tenant_id=:tenant AND panel_id=:panel
                    ORDER BY id
                    """
                ),
                {"tenant": context.tenant_id, "panel": panel_id},
            )
        ).mappings().all()
    return [
        {
            "role": row["role"],
            "content": row["content"],
            **({"result": row["result"]} if row["result"] is not None else {}),
        }
        for row in rows
    ]


def _manifest(
    panel_id: str,
    result: dict,
    source: str,
    prompt: str,
    parent_snapshot_id: str | None,
) -> dict:
    return {
        "schema": "workspace-model-revision-v1",
        "panel_id": panel_id,
        "requested_parent_snapshot_id": parent_snapshot_id,
        "source": source,
        "prompt": prompt,
        "code": result.get("code") or "",
        "result": result,
        "files": result.get("files") or {},
        "params": result.get("params"),
        "parameters": result.get("parameters"),
        "validation": result.get("validation"),
        "inspect_report": result.get("inspect_report"),
        "repair_history": result.get("repair_history") or [],
        "status": _snapshot_status(result),
    }


def _snapshot_status(result: dict) -> str:
    if not result.get("success"):
        return "fail"
    verdict = (result.get("inspect_report") or {}).get("verdict")
    return verdict if verdict in {"pass", "warn", "fail"} else "unknown"


def _snapshot_from_revision(
    row: Any,
    *,
    snapshot_id: str | None = None,
    metadata: Any | None = None,
) -> dict:
    manifest = dict(row["manifest"])
    details = metadata or {}
    inspect_report = manifest.get("inspect_report")
    return {
        "id": snapshot_id or str(row["id"]),
        "panel_id": manifest.get("panel_id") or details.get("panel_id"),
        "parent_snapshot_id": (
            str(row["parent_revision_id"])
            if row["parent_revision_id"]
            else manifest.get("legacy_parent_snapshot_id")
        ),
        "version": details.get("legacy_version") or row["revision_number"],
        "source": details.get("source") or manifest.get("source") or "legacy",
        "prompt": details.get("prompt") or manifest.get("prompt") or "",
        "code": manifest.get("code") or "",
        "result": manifest.get("result") or {},
        "files": manifest.get("files") or {},
        "params": manifest.get("params"),
        "parameters": manifest.get("parameters"),
        "validation": manifest.get("validation"),
        "inspect_report": inspect_report,
        "repair_history": manifest.get("repair_history") or [],
        "status": details.get("status") or manifest.get("status")
        or manifest.get("legacy_status") or "unknown",
        "created_at": _iso(row["created_at"]),
        "available_exports": (inspect_report or {}).get(
            "available_exports",
            [],
        ),
        "inspect_verdict": (inspect_report or {}).get("verdict"),
    }


async def _panel_project(connection, tenant_id: UUID, panel_id: str):
    return (
        await connection.execute(
            text(
                """
                SELECT s.project_id
                FROM workspace_panels p
                JOIN workspace_sessions s
                  ON s.tenant_id=p.tenant_id AND s.id=p.session_id
                WHERE p.tenant_id=:tenant AND p.id=:panel
                """
            ),
            {"tenant": tenant_id, "panel": panel_id},
        )
    ).mappings().one_or_none()


async def create_model_snapshot(
    panel_id: str,
    result: dict,
    source: str,
    prompt: str = "",
    parent_snapshot_id: str | None = None,
) -> dict:
    context = current_principal()
    revision_id = uuid4()
    branch_id = _stable_uuid("panel-branch", context, panel_id)
    manifest = _manifest(
        panel_id,
        result,
        source,
        prompt,
        parent_snapshot_id,
    )
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        panel = await _panel_project(connection, context.tenant_id, panel_id)
        if panel is None:
            raise ValueError("panel not found")
        branch = (
            await connection.execute(
                text(
                    """
                    SELECT head_revision_id, next_revision_number
                    FROM project_branches
                    WHERE tenant_id=:tenant AND id=:branch
                    FOR UPDATE
                    """
                ),
                {"tenant": context.tenant_id, "branch": branch_id},
            )
        ).mappings().one_or_none()
        if branch is None:
            revision_number = 1
            parent_revision_id = None
            await connection.execute(
                text(
                    """
                    INSERT INTO project_branches (
                        id, tenant_id, project_id, name,
                        next_revision_number, created_by_principal_id
                    ) VALUES (
                        :id, :tenant, :project, :name, 2, :principal
                    )
                    """
                ),
                {
                    "id": branch_id,
                    "tenant": context.tenant_id,
                    "project": panel["project_id"],
                    "name": "panel-"
                    + hashlib.sha256(panel_id.encode()).hexdigest()[:16],
                    "principal": context.principal_id,
                },
            )
            kind = "initial"
        else:
            revision_number = int(branch["next_revision_number"])
            parent_revision_id = branch["head_revision_id"]
            kind = "candidate"
        await connection.execute(
            text(
                """
                INSERT INTO project_revisions (
                    id, tenant_id, project_id, branch_id,
                    parent_revision_id, revision_number, kind,
                    content_hash, manifest, created_by_principal_id
                ) VALUES (
                    :id, :tenant, :project, :branch, :parent, :number,
                    :kind, :hash, CAST(:manifest AS jsonb), :principal
                )
                """
            ),
            {
                "id": revision_id,
                "tenant": context.tenant_id,
                "project": panel["project_id"],
                "branch": branch_id,
                "parent": parent_revision_id,
                "number": revision_number,
                "kind": kind,
                "hash": canonical_sha256(manifest),
                "manifest": _json(manifest),
                "principal": context.principal_id,
            },
        )
        await connection.execute(
            text(
                """
                UPDATE project_branches
                SET head_revision_id=:revision,
                    next_revision_number=:next,
                    updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=:tenant AND id=:branch
                """
            ),
            {
                "revision": revision_id,
                "next": revision_number + 1,
                "tenant": context.tenant_id,
                "branch": branch_id,
            },
        )
        await connection.execute(
            text(
                """
                UPDATE workspace_panels
                SET current_code=:code, current_params=CAST(:params AS jsonb)
                WHERE tenant_id=:tenant AND id=:panel
                """
            ),
            {
                "code": manifest["code"],
                "params": (
                    _json(manifest["params"])
                    if manifest["params"] is not None
                    else None
                ),
                "tenant": context.tenant_id,
                "panel": panel_id,
            },
        )
        row = (
            await connection.execute(
                text("SELECT * FROM project_revisions WHERE id=:id"),
                {"id": revision_id},
            )
        ).mappings().one()
    return _snapshot_from_revision(row)


async def list_model_snapshots(panel_id: str) -> list[dict]:
    context = current_principal()
    branch_id = _stable_uuid("panel-branch", context, panel_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        live = (
            await connection.execute(
                text(
                    """
                    SELECT * FROM project_revisions
                    WHERE tenant_id=:tenant AND branch_id=:branch
                    ORDER BY revision_number DESC
                    """
                ),
                {"tenant": context.tenant_id, "branch": branch_id},
            )
        ).mappings().all()
        imported = (
            await connection.execute(
                text(
                    """
                    SELECT r.*, m.legacy_snapshot_id, m.panel_id,
                           m.legacy_version, m.source, m.prompt, m.status
                    FROM legacy_snapshot_mappings m
                    JOIN project_revisions r
                      ON r.tenant_id=m.tenant_id
                     AND r.project_id=m.project_id
                     AND r.id=m.revision_id
                    WHERE m.tenant_id=:tenant AND m.panel_id=:panel
                    ORDER BY m.legacy_version DESC
                    """
                ),
                {"tenant": context.tenant_id, "panel": panel_id},
            )
        ).mappings().all()
    snapshots = [_snapshot_from_revision(row) for row in live]
    snapshots.extend(
        _snapshot_from_revision(
            row,
            snapshot_id=row["legacy_snapshot_id"],
            metadata=row,
        )
        for row in imported
    )
    return sorted(
        snapshots,
        key=lambda item: (item["created_at"], item["version"]),
        reverse=True,
    )


async def get_model_snapshot(snapshot_id: str) -> dict | None:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        try:
            revision_id = UUID(snapshot_id)
        except ValueError:
            revision_id = None
        row = None
        if revision_id is not None:
            row = (
                await connection.execute(
                    text(
                        "SELECT * FROM project_revisions "
                        "WHERE tenant_id=:tenant AND id=:id"
                    ),
                    {"tenant": context.tenant_id, "id": revision_id},
                )
            ).mappings().one_or_none()
        if row is not None:
            return _snapshot_from_revision(row)
        imported = (
            await connection.execute(
                text(
                    """
                    SELECT r.*, m.legacy_snapshot_id, m.panel_id,
                           m.legacy_version, m.source, m.prompt, m.status
                    FROM legacy_snapshot_mappings m
                    JOIN project_revisions r
                      ON r.tenant_id=m.tenant_id
                     AND r.project_id=m.project_id
                     AND r.id=m.revision_id
                    WHERE m.tenant_id=:tenant
                      AND m.legacy_snapshot_id=:snapshot
                    """
                ),
                {"tenant": context.tenant_id, "snapshot": snapshot_id},
            )
        ).mappings().one_or_none()
    if imported is None:
        return None
    return _snapshot_from_revision(
        imported,
        snapshot_id=snapshot_id,
        metadata=imported,
    )


async def snapshot_belongs_to_user(
    snapshot_id: str,
    user_id: str | None,
) -> bool:
    context = _context(user_id)
    # Bind explicitly for callers that do not pass through an HTTP dependency.
    from app.principal_context import bind_principal

    bind_principal(context)
    return await get_model_snapshot(snapshot_id) is not None


async def restore_model_snapshot(
    snapshot_id: str,
    user_id: str | None = None,
) -> dict | None:
    context = _context(user_id)
    from app.principal_context import bind_principal

    bind_principal(context)
    snapshot = await get_model_snapshot(snapshot_id)
    if snapshot is None:
        return None
    panel_id = snapshot["panel_id"]
    target_revision_id: UUID | None
    try:
        target_revision_id = UUID(snapshot_id)
    except ValueError:
        target_revision_id = None
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        if target_revision_id is None:
            target_revision_id = await connection.scalar(
                text(
                    """
                    SELECT revision_id FROM legacy_snapshot_mappings
                    WHERE tenant_id=:tenant
                      AND legacy_snapshot_id=:snapshot
                    """
                ),
                {"tenant": context.tenant_id, "snapshot": snapshot_id},
            )
        target = (
            await connection.execute(
                text(
                    """
                    SELECT * FROM project_revisions
                    WHERE tenant_id=:tenant AND id=:id
                    """
                ),
                {"tenant": context.tenant_id, "id": target_revision_id},
            )
        ).mappings().one_or_none()
        if target is None:
            return None
        branch = (
            await connection.execute(
                text(
                    """
                    SELECT head_revision_id, next_revision_number
                    FROM project_branches
                    WHERE tenant_id=:tenant AND id=:branch
                    FOR UPDATE
                    """
                ),
                {"tenant": context.tenant_id, "branch": target["branch_id"]},
            )
        ).mappings().one()
        revision_id = uuid4()
        manifest = {
            **dict(target["manifest"]),
            "schema": "workspace-restored-revision-v1",
            "restored_from_revision_id": str(target_revision_id),
            "panel_id": panel_id,
        }
        await connection.execute(
            text(
                """
                INSERT INTO project_revisions (
                    id, tenant_id, project_id, branch_id,
                    parent_revision_id, revision_number, kind,
                    content_hash, manifest, created_by_principal_id
                ) VALUES (
                    :id, :tenant, :project, :branch, :parent, :number,
                    'rollback', :hash, CAST(:manifest AS jsonb), :principal
                )
                """
            ),
            {
                "id": revision_id,
                "tenant": context.tenant_id,
                "project": target["project_id"],
                "branch": target["branch_id"],
                "parent": branch["head_revision_id"],
                "number": branch["next_revision_number"],
                "hash": canonical_sha256(manifest),
                "manifest": _json(manifest),
                "principal": context.principal_id,
            },
        )
        await connection.execute(
            text(
                """
                UPDATE project_branches SET head_revision_id=:revision,
                    next_revision_number=:next,
                    updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=:tenant AND id=:branch
                """
            ),
            {
                "revision": revision_id,
                "next": int(branch["next_revision_number"]) + 1,
                "tenant": context.tenant_id,
                "branch": target["branch_id"],
            },
        )
        await connection.execute(
            text(
                """
                UPDATE workspace_panels SET current_code=:code,
                    current_params=CAST(:params AS jsonb)
                WHERE tenant_id=:tenant AND id=:panel
                """
            ),
            {
                "code": snapshot["code"],
                "params": (
                    _json(snapshot["params"])
                    if snapshot["params"] is not None
                    else None
                ),
                "tenant": context.tenant_id,
                "panel": panel_id,
            },
        )
        restored_result = {
            **snapshot["result"],
            "snapshot_id": snapshot["id"],
            "version": snapshot["version"],
            "panel_id": panel_id,
        }
        await connection.execute(
            text(
                """
                INSERT INTO workspace_messages (
                    tenant_id, panel_id, role, content, result
                ) VALUES (
                    :tenant, :panel, 'assistant', :content,
                    CAST(:result AS jsonb)
                )
                """
            ),
            {
                "tenant": context.tenant_id,
                "panel": panel_id,
                "content": f"已恢复模型版本 v{snapshot['version']}",
                "result": _json(restored_result),
            },
        )
    snapshot["result"] = restored_result
    return snapshot


async def _request_project(connection, tenant_id: UUID, request_id: str):
    return await connection.scalar(
        text(
            """
            SELECT project_id FROM project_files
            WHERE tenant_id=:tenant AND request_id=:request
            ORDER BY created_at DESC LIMIT 1
            """
        ),
        {"tenant": tenant_id, "request": request_id},
    )


async def save_feedback(
    request_id: str,
    rating: str | None = None,
    printed: str | None = None,
    note: str | None = None,
) -> dict:
    context = current_principal()
    feedback_id = uuid4()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        project_id = await _request_project(
            connection,
            context.tenant_id,
            request_id,
        )
        row = (
            await connection.execute(
                text(
                    """
                    INSERT INTO product_feedback (
                        id, tenant_id, principal_id, project_id,
                        request_id, rating, printed, note
                    ) VALUES (
                        :id, :tenant, :principal, :project,
                        :request, :rating, :printed, :note
                    )
                    RETURNING request_id, rating, printed, note, created_at
                    """
                ),
                {
                    "id": feedback_id,
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                    "project": project_id,
                    "request": request_id,
                    "rating": rating,
                    "printed": printed,
                    "note": note,
                },
            )
        ).mappings().one()
    return {**dict(row), "created_at": _iso(row["created_at"])}


async def get_feedback(request_id: str) -> list[dict]:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT request_id, rating, printed, note, created_at
                    FROM product_feedback
                    WHERE tenant_id=:tenant AND request_id=:request
                    ORDER BY created_at
                    """
                ),
                {"tenant": context.tenant_id, "request": request_id},
            )
        ).mappings().all()
    return [
        {**dict(row), "created_at": _iso(row["created_at"])}
        for row in rows
    ]


def _link(row: Any, user_id: str | None = None) -> dict:
    return {
        **{
            key: row[key]
            for key in (
                "id",
                "request_id",
                "status",
                "document_id",
                "workspace_id",
                "element_id",
                "translation_id",
                "document_name",
                "mode",
            )
            if key in row
        },
        "user_id": user_id,
        "onshape_url": row["external_url"],
        "step_filename": row["source_filename"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


async def save_onshape_link(
    request_id: str,
    user_id: str | None,
    document_id: str,
    workspace_id: str,
    element_id: str | None,
    translation_id: str | None,
    status: str,
    onshape_url: str,
    document_name: str = "",
    step_filename: str = "",
    mode: str = "import_step",
    raw_response: dict | None = None,
) -> dict:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        project_id = await _request_project(
            connection,
            context.tenant_id,
            request_id,
        )
        row = (
            await connection.execute(
                text(
                    """
                    INSERT INTO connector_links (
                        tenant_id, principal_id, project_id, connector,
                        request_id, document_id, workspace_id, element_id,
                        translation_id, status, external_url, document_name,
                        source_filename, mode, raw_response
                    ) VALUES (
                        :tenant, :principal, :project, 'onshape',
                        :request, :document, :workspace, :element,
                        :translation, :status, :url, :name,
                        :filename, :mode, CAST(:raw AS jsonb)
                    )
                    RETURNING *
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                    "project": project_id,
                    "request": request_id,
                    "document": document_id,
                    "workspace": workspace_id,
                    "element": element_id,
                    "translation": translation_id,
                    "status": status,
                    "url": onshape_url,
                    "name": document_name,
                    "filename": step_filename,
                    "mode": mode,
                    "raw": (
                        _json(raw_response)
                        if raw_response is not None
                        else None
                    ),
                },
            )
        ).mappings().one()
    return _link(row, user_id)


async def get_onshape_links(
    request_id: str,
    user_id: str | None = None,
) -> list[dict]:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT * FROM connector_links
                    WHERE tenant_id=:tenant AND connector='onshape'
                      AND request_id=:request
                    ORDER BY updated_at DESC, id DESC
                    """
                ),
                {"tenant": context.tenant_id, "request": request_id},
            )
        ).mappings().all()
    return [_link(row, user_id) for row in rows]


async def get_latest_onshape_link(
    request_id: str,
    user_id: str | None = None,
) -> dict | None:
    rows = await get_onshape_links(request_id, user_id)
    return rows[0] if rows else None


async def get_onshape_link_by_translation(
    translation_id: str,
    user_id: str | None = None,
) -> dict | None:
    context = _context(user_id)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT * FROM connector_links
                    WHERE tenant_id=:tenant AND connector='onshape'
                      AND translation_id=:translation
                    ORDER BY updated_at DESC, id DESC LIMIT 1
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "translation": translation_id,
                },
            )
        ).mappings().one_or_none()
    return _link(row, user_id) if row else None


async def update_onshape_link_status(
    link_id: int,
    status: str,
    element_id: str | None,
    onshape_url: str,
    raw_response: dict | None = None,
) -> dict | None:
    context = current_principal()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        row = (
            await connection.execute(
                text(
                    """
                    UPDATE connector_links
                    SET status=:status,
                        element_id=COALESCE(:element, element_id),
                        external_url=:url,
                        raw_response=COALESCE(
                            CAST(:raw AS jsonb),
                            raw_response
                        ),
                        updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=:tenant AND id=:id
                    RETURNING *
                    """
                ),
                {
                    "status": status,
                    "element": element_id,
                    "url": onshape_url,
                    "raw": (
                        _json(raw_response)
                        if raw_response is not None
                        else None
                    ),
                    "tenant": context.tenant_id,
                    "id": link_id,
                },
            )
        ).mappings().one_or_none()
    return _link(row) if row else None
