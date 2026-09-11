"""Deterministic import of SQLite/filesystem product data into M1 stores.

The importer reads legacy sources without mutating them.  Every entity receives
an immutable source checksum and deterministic target identity.  Replaying the
same snapshot is a no-op; changing bytes behind an already imported source ID is
reported as a collision instead of silently overwriting commercial data.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID, uuid5

from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import auth_transaction, get_database_engine, tenant_transaction
from app.domain.identity import (
    IDENTITY_NAMESPACE,
    PrincipalContext,
    local_anonymous_principal,
    platform_service_principal,
    quarantine_principal,
    user_principal,
)
from app.execution.canonical import canonical_sha256
from app.object_store import (
    delete_object,
    head_object,
    put_file,
    sha256_object,
)
from app.repositories.identity import ensure_principal


STORE_INVENTORY: dict[str, dict[str, Any]] = {
    "auth": {
        "source": "auth.db",
        "entities": (
            "users",
            "auth_sessions",
            "verification_codes",
            "invite_codes",
        ),
        "target": "PostgreSQL auth_* tables through cad_agent_auth",
    },
    "history": {
        "source": "history.db",
        "entities": ("sessions", "panels", "messages"),
        "target": "workspace_sessions/workspace_panels/workspace_messages",
    },
    "snapshots": {
        "source": "history.db:model_snapshots",
        "entities": ("model_snapshots",),
        "target": "ProjectRevision ancestry + ID-only legacy_snapshot_mappings",
    },
    "local_workflows": {
        "source": "history.db",
        "entities": ("local_workflow_runs", "local_workflow_events"),
        "target": "workflow_runs/task_events",
    },
    "feedback": {
        "source": "history.db:feedback",
        "entities": ("feedback",),
        "target": "product_feedback",
    },
    "onshape": {
        "source": "history.db:onshape_links",
        "entities": ("onshape_links",),
        "target": "connector_links",
    },
    "generated_files": {
        "source": "FILE_STORAGE_DIR",
        "entities": ("request files",),
        "target": "S3 immutable objects + project_files",
    },
    "dfm": {
        "source": "dfm_rules.db",
        "entities": ("rule_sets", "rules"),
        "target": "dfm_rule_sets/dfm_rules",
    },
    "knowledge": {
        "source": "knowledge_graph.db",
        "entities": ("kg_nodes", "kg_edges"),
        "target": "knowledge_nodes/knowledge_edges",
    },
    "fusion_runtime": {
        "source": "fusion360/runtime.db",
        "entities": ("connectors", "tasks", "approvals", "snapshots"),
        "target": "connector_records",
    },
    "fusion_agent": {
        "source": "fusion360/agent-audit.db + agent-artifacts",
        "entities": (
            "agent_plans",
            "agent_reports",
            "agent_connectors",
            "agent_artifacts",
        ),
        "target": "connector_records/connector_audit_records/connector_artifacts",
    },
    "fusion_tokens": {
        "source": "fusion360/tokens.db",
        "entities": ("oauth_tokens", "oauth_states"),
        "target": "connector_tokens/connector_oauth_states (encrypted bytes only)",
    },
    "fusion_artifacts": {
        "source": "FUSION_ARTIFACT_ROOT",
        "entities": ("request files",),
        "target": "S3 immutable objects + connector_artifacts",
    },
    "capability_artifacts": {
        "source": "FILE_STORAGE_DIR/capability_users",
        "entities": ("uploads", "runs"),
        "target": "S3 immutable objects + capability_artifacts",
    },
}


SQLITE_TABLES: dict[str, tuple[str, ...]] = {
    "auth": (
        "users",
        "auth_sessions",
        "verification_codes",
        "invite_codes",
    ),
    "history": (
        "sessions",
        "panels",
        "messages",
        "model_snapshots",
        "feedback",
        "onshape_links",
        "local_workflow_runs",
        "local_workflow_events",
    ),
    "dfm": ("rule_sets", "rules"),
    "knowledge": ("kg_nodes", "kg_edges"),
    "fusion_runtime": ("connectors", "tasks", "approvals", "snapshots"),
    "fusion_agent": (
        "agent_plans",
        "agent_reports",
        "agent_artifacts",
        "agent_connectors",
    ),
    "fusion_tokens": ("oauth_tokens", "oauth_states"),
}


class LegacyImportError(RuntimeError):
    """Base class for operator-visible import failures."""


class LegacyImportCollision(LegacyImportError):
    """A source ID was already imported from different bytes."""


class LegacyRollbackUnsafe(LegacyImportError):
    """Imported data acquired live descendants and can no longer be rolled back."""


@dataclass(frozen=True, slots=True)
class LegacySourcePaths:
    auth_db: Path | None = None
    history_db: Path | None = None
    dfm_db: Path | None = None
    knowledge_db: Path | None = None
    generated_files_root: Path | None = None
    fusion_runtime_db: Path | None = None
    fusion_agent_db: Path | None = None
    fusion_token_db: Path | None = None
    fusion_runtime_artifact_root: Path | None = None
    fusion_agent_artifact_root: Path | None = None
    capability_artifact_root: Path | None = None

    @classmethod
    def from_settings(cls) -> "LegacySourcePaths":
        history = Path(settings.history_db_path).expanduser()
        files = Path(settings.file_storage_dir).expanduser()
        return cls(
            auth_db=history.with_name("auth.db"),
            history_db=history,
            dfm_db=history.with_name("dfm_rules.db"),
            knowledge_db=history.with_name("knowledge_graph.db"),
            generated_files_root=files,
            fusion_runtime_db=Path(
                os.environ.get(
                    "FUSION_RUNTIME_DB",
                    str(history.parent / "fusion360" / "runtime.db"),
                )
            ).expanduser(),
            fusion_agent_db=Path(settings.fusion_agent_db_path).expanduser(),
            fusion_token_db=Path(settings.fusion_token_db_path).expanduser(),
            fusion_runtime_artifact_root=Path(
                os.environ.get(
                    "FUSION_ARTIFACT_ROOT",
                    str(history.parent / "fusion360" / "artifacts"),
                )
            ).expanduser(),
            fusion_agent_artifact_root=Path(
                settings.fusion_agent_artifact_dir
            ).expanduser(),
            capability_artifact_root=files / "capability_users",
        )


@dataclass(frozen=True, slots=True)
class LegacyFile:
    store: str
    relative_path: str
    path: Path = field(repr=False)
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class LegacySnapshot:
    rows: dict[str, dict[str, tuple[dict[str, Any], ...]]]
    files: tuple[LegacyFile, ...]
    source_fingerprint: str

    @property
    def source_counts(self) -> dict[str, int]:
        counts = {
            f"{store}.{table}": len(items)
            for store, tables in sorted(self.rows.items())
            for table, items in sorted(tables.items())
        }
        for store in sorted({item.store for item in self.files}):
            counts[f"{store}.files"] = sum(
                item.store == store for item in self.files
            )
        return counts


@dataclass(frozen=True, slots=True)
class LegacyImportReport:
    source_fingerprint: str
    source_counts: dict[str, int]
    target_counts: dict[str, int]
    quarantined_source_count: int
    object_count: int
    object_bytes: int
    status: str = "succeeded"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _ImportState:
    snapshot: LegacySnapshot
    target_counts: dict[str, int] = field(default_factory=dict)
    quarantined_sources: set[str] = field(default_factory=set)
    object_count: int = 0
    object_bytes: int = 0
    tenant_contexts: dict[UUID, PrincipalContext] = field(default_factory=dict)
    sessions: dict[str, tuple[PrincipalContext, UUID]] = field(default_factory=dict)
    panels: dict[str, tuple[PrincipalContext, UUID]] = field(default_factory=dict)
    requests: dict[str, tuple[PrincipalContext, UUID, UUID | None]] = field(
        default_factory=dict
    )

    def counted(self, target: str, amount: int = 1) -> None:
        self.target_counts[target] = self.target_counts.get(target, 0) + amount

    def remember(self, context: PrincipalContext) -> None:
        self.tenant_contexts.setdefault(context.tenant_id, context)


_DB_FIELD_NAMES = {
    "auth": "auth_db",
    "history": "history_db",
    "dfm": "dfm_db",
    "knowledge": "knowledge_db",
    "fusion_runtime": "fusion_runtime_db",
    "fusion_agent": "fusion_agent_db",
    "fusion_tokens": "fusion_token_db",
}

_FILE_ROOTS = {
    "generated_files": "generated_files_root",
    "fusion_artifacts": "fusion_runtime_artifact_root",
    "fusion_agent_artifacts": "fusion_agent_artifact_root",
    "capability_artifacts": "capability_artifact_root",
}

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _stable_uuid(kind: str, *parts: object) -> UUID:
    value = ":".join(str(part) for part in parts)
    return uuid5(IDENTITY_NAMESPACE, f"legacy-import:{kind}:{value}")


def _dt(value: Any, *, default: datetime | None = None) -> datetime:
    if value is None:
        return default or datetime.now(timezone.utc)
    if isinstance(value, datetime):
        resolved = value
    elif isinstance(value, (float, int)):
        resolved = datetime.fromtimestamp(float(value), timezone.utc)
    else:
        resolved = datetime.fromisoformat(str(value))
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved


def _load_json(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _dump_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _safe_for_fingerprint(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "$bytes_sha256": hashlib.sha256(value).hexdigest(),
            "$size": len(value),
        }
    if isinstance(value, dict):
        return {
            str(key): _safe_for_fingerprint(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (list, tuple)):
        return [_safe_for_fingerprint(item) for item in value]
    return value


def _content_sha(value: Any) -> str:
    return canonical_sha256(_safe_for_fingerprint(value))


def _read_sqlite(path: Path | None, tables: Iterable[str]) -> dict[str, tuple[dict, ...]]:
    result = {table: () for table in tables}
    if path is None or not path.is_file():
        return result
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        available = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table in tables:
            if not _SAFE_IDENTIFIER.fullmatch(table) or table not in available:
                continue
            rows = connection.execute(
                f'SELECT * FROM "{table}"'
            ).fetchall()
            result[table] = tuple(dict(row) for row in rows)
        connection.execute("ROLLBACK")
    finally:
        connection.close()
    return result


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _scan_root(store: str, root: Path | None) -> tuple[LegacyFile, ...]:
    if root is None or not root.is_dir():
        return ()
    resolved_root = root.resolve()
    items: list[LegacyFile] = []
    for path in sorted(resolved_root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(resolved_root).as_posix()
        except ValueError as exc:
            raise LegacyImportError(
                f"{store} file escaped its configured root"
            ) from exc
        size, digest = _hash_file(resolved)
        items.append(
            LegacyFile(
                store=store,
                relative_path=relative,
                path=resolved,
                size_bytes=size,
                sha256=digest,
            )
        )
    return tuple(items)


def capture_legacy_snapshot(paths: LegacySourcePaths) -> LegacySnapshot:
    rows = {
        store: _read_sqlite(
            getattr(paths, _DB_FIELD_NAMES[store]),
            tables,
        )
        for store, tables in SQLITE_TABLES.items()
    }
    files = tuple(
        item
        for store, field_name in _FILE_ROOTS.items()
        for item in _scan_root(store, getattr(paths, field_name))
    )
    fingerprint_payload = {
        "schema": "cad-agent-legacy-import-v1",
        "rows": _safe_for_fingerprint(rows),
        "files": [
            {
                "store": item.store,
                "relative_path": item.relative_path,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
            }
            for item in files
        ],
    }
    return LegacySnapshot(
        rows=rows,
        files=files,
        source_fingerprint=canonical_sha256(fingerprint_payload),
    )


async def _ensure_context(
    connection,
    context: PrincipalContext,
    *,
    display_name: str,
) -> None:
    await ensure_principal(connection, context, display_name=display_name)


async def _ensure_import_run(
    connection,
    state: _ImportState,
    context: PrincipalContext,
) -> UUID:
    run_id = _stable_uuid(
        "import-run",
        state.snapshot.source_fingerprint,
        context.tenant_id,
    )
    await connection.execute(
        text(
            """
            INSERT INTO legacy_import_runs (
                id, tenant_id, source_fingerprint, status, report
            ) VALUES (
                :id, :tenant, :fingerprint, 'running', '{}'::jsonb
            )
            ON CONFLICT (tenant_id, source_fingerprint)
            DO UPDATE SET status='running', completed_at=NULL
            """
        ),
        {
            "id": run_id,
            "tenant": context.tenant_id,
            "fingerprint": state.snapshot.source_fingerprint,
        },
    )
    state.remember(context)
    return run_id


async def _mapping_status(
    connection,
    *,
    tenant_id: UUID,
    source_store: str,
    entity_type: str,
    source_id: str,
    content_sha256: str,
    target_table: str,
    target_id: str | None,
) -> bool:
    row = (
        await connection.execute(
            text(
                """
                SELECT content_sha256, target_table, target_id
                FROM legacy_import_mappings
                WHERE tenant_id=:tenant
                  AND source_store=:store
                  AND entity_type=:entity
                  AND source_id=:source_id
                """
            ),
            {
                "tenant": tenant_id,
                "store": source_store,
                "entity": entity_type,
                "source_id": source_id,
            },
        )
    ).mappings().one_or_none()
    if row is None:
        return False
    if (
        row["content_sha256"] != content_sha256
        or row["target_table"] != target_table
        or (target_id is not None and row["target_id"] != target_id)
    ):
        raise LegacyImportCollision(
            f"{source_store}.{entity_type}:{source_id} was already "
            "imported from different content"
        )
    return True


async def _record_mapping(
    connection,
    state: _ImportState,
    context: PrincipalContext,
    *,
    source_store: str,
    entity_type: str,
    source_id: str,
    target_table: str,
    target_id: object,
    content_sha256: str,
) -> None:
    run_id = await _ensure_import_run(connection, state, context)
    await connection.execute(
        text(
            """
            INSERT INTO legacy_import_mappings (
                tenant_id, source_store, entity_type, source_id,
                target_table, target_id, content_sha256, import_run_id
            ) VALUES (
                :tenant, :store, :entity, :source_id,
                :target_table, :target_id, :checksum, :run_id
            )
            ON CONFLICT (
                tenant_id, source_store, entity_type, source_id
            ) DO NOTHING
            """
        ),
        {
            "tenant": context.tenant_id,
            "store": source_store,
            "entity": entity_type,
            "source_id": source_id,
            "target_table": target_table,
            "target_id": str(target_id),
            "checksum": content_sha256,
            "run_id": run_id,
        },
    )


async def _ensure_project(
    connection,
    context: PrincipalContext,
    *,
    source_store: str,
    source_id: str,
    name: str,
    created_at: datetime | None = None,
) -> UUID:
    project_id = _stable_uuid(
        "project",
        context.tenant_id,
        source_store,
        source_id,
    )
    slug = f"legacy-{hashlib.sha256(f'{source_store}:{source_id}'.encode()).hexdigest()[:20]}"
    await connection.execute(
        text(
            """
            INSERT INTO projects (
                id, tenant_id, name, slug, created_by_principal_id,
                created_at, updated_at
            ) VALUES (
                :id, :tenant, :name, :slug, :principal,
                :created_at, :created_at
            )
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "id": project_id,
            "tenant": context.tenant_id,
            "name": name.strip() or "导入的 CAD 项目",
            "slug": slug,
            "principal": context.principal_id,
            "created_at": created_at or datetime.now(timezone.utc),
        },
    )
    stored = (
        await connection.execute(
            text(
                """
                SELECT tenant_id, created_by_principal_id
                FROM projects WHERE id=:id
                """
            ),
            {"id": project_id},
        )
    ).mappings().one()
    if (
        stored["tenant_id"] != context.tenant_id
        or stored["created_by_principal_id"] != context.principal_id
    ):
        raise LegacyImportCollision(
            f"deterministic project ID collision for {source_store}:{source_id}"
        )
    await connection.execute(
        text(
            """
            INSERT INTO project_memberships (
                tenant_id, project_id, principal_id, role
            ) VALUES (:tenant, :project, :principal, 'owner')
            ON CONFLICT (tenant_id, project_id, principal_id)
            DO UPDATE SET role='owner'
            """
        ),
        {
            "tenant": context.tenant_id,
            "project": project_id,
            "principal": context.principal_id,
        },
    )
    return project_id


def _session_context(
    session: dict[str, Any],
    imported_user_ids: set[str],
) -> PrincipalContext:
    user_id = str(session.get("user_id") or "").strip()
    if user_id and user_id in imported_user_ids:
        return user_principal(user_id)
    if not user_id:
        return local_anonymous_principal()
    source = f"history-session:{session['id']}:{user_id or 'ownerless'}"
    return quarantine_principal(source)


async def _import_auth(state: _ImportState) -> None:
    rows = state.snapshot.rows["auth"]
    imported_users = {str(row["id"]): row for row in rows["users"]}
    for source_id, row in sorted(imported_users.items()):
        context = user_principal(source_id)
        checksum = _content_sha(row)
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
            role="migrator",
        ) as connection:
            await _ensure_context(
                connection,
                context,
                display_name=str(row.get("phone") or source_id),
            )
            replayed = await _mapping_status(
                connection,
                tenant_id=context.tenant_id,
                source_store="auth",
                entity_type="users",
                source_id=source_id,
                content_sha256=checksum,
                target_table="auth_users",
                target_id=source_id,
            )
            if not replayed:
                await connection.execute(
                    text(
                        """
                        INSERT INTO auth_users (
                            id, tenant_id, principal_id, phone,
                            phone_lookup_hash, password_hash,
                            registered_via, created_at, last_login_at,
                            updated_at
                        ) VALUES (
                            :id, :tenant, :principal, :phone,
                            :phone_hash, :password_hash, :registered_via,
                            :created_at, :last_login_at, :last_login_at
                        )
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {
                        "id": source_id,
                        "tenant": context.tenant_id,
                        "principal": context.principal_id,
                        "phone": row["phone"],
                        "phone_hash": hashlib.sha256(
                            str(row["phone"]).encode("utf-8")
                        ).hexdigest(),
                        "password_hash": row["password_hash"],
                        "registered_via": row["registered_via"],
                        "created_at": _dt(row.get("created_at")),
                        "last_login_at": _dt(row.get("last_login_at")),
                    },
                )
                await _record_mapping(
                    connection,
                    state,
                    context,
                    source_store="auth",
                    entity_type="users",
                    source_id=source_id,
                    target_table="auth_users",
                    target_id=source_id,
                    content_sha256=checksum,
                )
            state.counted("auth_users")

    # Verification and invite codes are global authentication-service state,
    # not tenant-owned user data.  Import them into the same stable service
    # principal used by the live PostgreSQL auth repository.
    platform = platform_service_principal("authentication")
    async with tenant_transaction(
        platform.tenant_id,
        platform.principal_id,
        role="migrator",
    ) as connection:
        await _ensure_context(
            connection,
            platform,
            display_name="待认领认证数据",
        )
        await _ensure_import_run(connection, state, platform)

    for table_name in ("auth_sessions", "verification_codes", "invite_codes"):
        for row in rows[table_name]:
            if table_name == "auth_sessions":
                user_id = str(row["user_id"])
                if user_id not in imported_users:
                    context = quarantine_principal(
                        f"auth-session-user:{user_id}"
                    )
                    state.counted("auth_users")
                    state.quarantined_sources.add(
                        f"auth.auth_sessions:{row['token_id']}"
                    )
                else:
                    context = user_principal(user_id)
                source_id = str(row["token_id"])
                target_table = "auth_sessions"
            else:
                context = platform
                source_id = str(
                    row.get("id")
                    if table_name == "verification_codes"
                    else row["code"]
                )
                target_table = (
                    "auth_verification_codes"
                    if table_name == "verification_codes"
                    else "auth_invite_codes"
                )
            checksum = _content_sha(row)
            async with tenant_transaction(
                context.tenant_id,
                context.principal_id,
                role="migrator",
            ) as connection:
                await _ensure_context(
                    connection,
                    context,
                    display_name="导入的认证主体",
                )
                if await _mapping_status(
                    connection,
                    tenant_id=context.tenant_id,
                    source_store="auth",
                    entity_type=table_name,
                    source_id=source_id,
                    content_sha256=checksum,
                    target_table=target_table,
                    target_id=(
                        None
                        if table_name == "verification_codes"
                        else source_id
                    ),
                ):
                    state.counted(target_table)
                    continue
                if table_name == "invite_codes":
                    # The code namespace is globally unique.  The tenant-scoped
                    # migrator role intentionally cannot see another tenant's
                    # row, so use the narrowly privileged auth role after the
                    # deterministic replay check.
                    async with auth_transaction() as auth_connection:
                        existing_invite = await auth_connection.scalar(
                            text(
                                """
                                SELECT 1 FROM auth_invite_codes
                                WHERE code=:code
                                """
                            ),
                            {"code": row["code"]},
                        )
                    if existing_invite:
                        raise LegacyImportCollision(
                            "legacy invite code collides with existing "
                            f"authentication data: {row['code']}"
                        )
                generated_target_id: object = source_id
                if table_name == "auth_sessions":
                    user_exists = await connection.scalar(
                        text(
                            "SELECT 1 FROM auth_users "
                            "WHERE tenant_id=:tenant AND id=:id"
                        ),
                        {"tenant": context.tenant_id, "id": user_id},
                    )
                    if not user_exists:
                        missing_user_id = (
                            "quarantine-"
                            + hashlib.sha256(user_id.encode("utf-8")).hexdigest()
                        )
                        missing_user_checksum = _content_sha(
                            {
                                "missing_legacy_user_id": user_id,
                                "authentication_disabled": True,
                            }
                        )
                        if not await _mapping_status(
                            connection,
                            tenant_id=context.tenant_id,
                            source_store="auth",
                            entity_type="missing_users",
                            source_id=user_id,
                            content_sha256=missing_user_checksum,
                            target_table="auth_users",
                            target_id=missing_user_id,
                        ):
                            phone = f"quarantine:{hashlib.sha256(user_id.encode()).hexdigest()}"
                            await connection.execute(
                                text(
                                    """
                                    INSERT INTO auth_users (
                                        id, tenant_id, principal_id, phone,
                                        phone_lookup_hash, password_hash,
                                        registered_via, created_at,
                                        last_login_at, updated_at
                                    ) VALUES (
                                        :id, :tenant, :principal, :phone,
                                        :phone_hash, '!disabled!',
                                        'legacy_quarantine', :created,
                                        :created, :created
                                    )
                                    """
                                ),
                                {
                                    "id": missing_user_id,
                                    "tenant": context.tenant_id,
                                    "principal": context.principal_id,
                                    "phone": phone,
                                    "phone_hash": hashlib.sha256(
                                        phone.encode("utf-8")
                                    ).hexdigest(),
                                    "created": _dt(row.get("created_at")),
                                },
                            )
                            await _record_mapping(
                                connection,
                                state,
                                context,
                                source_store="auth",
                                entity_type="missing_users",
                                source_id=user_id,
                                target_table="auth_users",
                                target_id=missing_user_id,
                                content_sha256=missing_user_checksum,
                            )
                        user_id = missing_user_id
                    await connection.execute(
                        text(
                            """
                            INSERT INTO auth_sessions (
                                token_id, tenant_id, user_id, expires_at,
                                revoked_at, created_at
                            ) VALUES (
                                :token, :tenant, :user, :expires,
                                :revoked, :created
                            )
                            ON CONFLICT (token_id) DO NOTHING
                            """
                        ),
                        {
                            "token": row["token_id"],
                            "tenant": context.tenant_id,
                            "user": user_id,
                            "expires": _dt(row["expires_at"]),
                            "revoked": (
                                _dt(row["revoked_at"])
                                if row.get("revoked_at")
                                else None
                            ),
                            "created": _dt(row["created_at"]),
                        },
                    )
                elif table_name == "verification_codes":
                    generated_target_id = await connection.scalar(
                        text(
                            """
                            INSERT INTO auth_verification_codes (
                                tenant_id, phone_lookup_hash, purpose,
                                code_hash, expires_at, consumed_at, created_at
                            ) VALUES (
                                :tenant, :phone_hash, :purpose,
                                :code_hash, :expires, :consumed, :created
                            )
                            RETURNING id
                            """
                        ),
                        {
                            "tenant": context.tenant_id,
                            "phone_hash": hashlib.sha256(
                                str(row["phone"]).encode("utf-8")
                            ).hexdigest(),
                            "purpose": row["purpose"],
                            "code_hash": row["code_hash"],
                            "expires": _dt(row["expires_at"]),
                            "consumed": (
                                _dt(row["consumed_at"])
                                if row.get("consumed_at")
                                else None
                            ),
                            "created": _dt(row["created_at"]),
                        },
                    )
                else:
                    inserted = await connection.scalar(
                        text(
                            """
                            INSERT INTO auth_invite_codes (
                                tenant_id, code, max_uses, used_count,
                                expires_at, disabled_at, created_at
                            ) VALUES (
                                :tenant, :code, :max_uses, :used_count,
                                :expires, :disabled, :created
                            )
                            ON CONFLICT (code) DO NOTHING
                            RETURNING code
                            """
                        ),
                        {
                            "tenant": context.tenant_id,
                            "code": row["code"],
                            "max_uses": row["max_uses"],
                            "used_count": row["used_count"],
                            "expires": (
                                _dt(row["expires_at"])
                                if row.get("expires_at")
                                else None
                            ),
                            "disabled": (
                                _dt(row["disabled_at"])
                                if row.get("disabled_at")
                                else None
                            ),
                            "created": _dt(row["created_at"]),
                        },
                    )
                    if inserted is None:
                        raise LegacyImportCollision(
                            "legacy invite code was concurrently claimed: "
                            f"{row['code']}"
                        )
                await _record_mapping(
                    connection,
                    state,
                    context,
                    source_store="auth",
                    entity_type=table_name,
                    source_id=source_id,
                    target_table=target_table,
                    target_id=generated_target_id,
                    content_sha256=checksum,
                )
                state.counted(target_table)


async def _insert_project_revision_history(
    connection,
    state: _ImportState,
    context: PrincipalContext,
    *,
    project_id: UUID,
    panel: dict[str, Any],
    snapshots: list[dict[str, Any]],
) -> None:
    panel_id = str(panel["id"])
    branch_id = _stable_uuid("branch", context.tenant_id, panel_id)
    branch_name = (
        "panel-"
        + hashlib.sha256(panel_id.encode("utf-8")).hexdigest()[:16]
    )
    ordered = sorted(
        snapshots,
        key=lambda row: (int(row.get("version") or 0), str(row["id"])),
    )
    manifests: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    if ordered:
        for row in ordered:
            result = _load_json(row.get("result"), {})
            manifest = {
                "schema": "legacy-model-snapshot-v1",
                "legacy_snapshot_id": str(row["id"]),
                "legacy_parent_snapshot_id": row.get("parent_snapshot_id"),
                "source": row.get("source") or "legacy",
                "prompt": row.get("prompt") or "",
                "code": row.get("code") or "",
                "result": result,
                "files": _load_json(row.get("files"), {}),
                "params": _load_json(row.get("params")),
                "parameters": _load_json(row.get("parameters")),
                "validation": _load_json(row.get("validation")),
                "inspect_report": _load_json(row.get("inspect_report")),
                "repair_history": _load_json(
                    row.get("repair_history"),
                    [],
                ),
                "legacy_status": row.get("status") or "unknown",
            }
            manifests.append((manifest, row))
    else:
        manifests.append(
            (
                {
                    "schema": "legacy-empty-panel-v1",
                    "panel_id": panel_id,
                    "code": panel.get("current_code") or "",
                    "params": _load_json(panel.get("current_params")),
                },
                None,
            )
        )

    await connection.execute(
        text(
            """
            INSERT INTO project_branches (
                id, tenant_id, project_id, name, next_revision_number,
                created_by_principal_id
            ) VALUES (
                :id, :tenant, :project, :name, :next_number, :principal
            )
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "id": branch_id,
            "tenant": context.tenant_id,
            "project": project_id,
            "name": branch_name,
            "next_number": len(manifests) + 1,
            "principal": context.principal_id,
        },
    )

    previous: UUID | None = None
    revision_ids: list[UUID] = []
    for index, (manifest, source_row) in enumerate(manifests, start=1):
        source_identity = (
            str(source_row["id"])
            if source_row is not None
            else f"empty-panel:{panel_id}"
        )
        revision_id = _stable_uuid(
            "revision",
            context.tenant_id,
            source_identity,
        )
        revision_ids.append(revision_id)
        await connection.execute(
            text(
                """
                INSERT INTO project_revisions (
                    id, tenant_id, project_id, branch_id,
                    parent_revision_id, revision_number, kind,
                    content_hash, manifest, created_by_principal_id,
                    created_at
                ) VALUES (
                    :id, :tenant, :project, :branch,
                    :parent, :number, :kind,
                    :content_hash, CAST(:manifest AS jsonb), :principal,
                    :created_at
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": revision_id,
                "tenant": context.tenant_id,
                "project": project_id,
                "branch": branch_id,
                "parent": previous,
                "number": index,
                "kind": "initial" if index == 1 else "imported",
                "content_hash": canonical_sha256(manifest),
                "manifest": _dump_json(manifest),
                "principal": context.principal_id,
                "created_at": _dt(
                    source_row.get("created_at") if source_row else panel.get("created_at")
                ),
            },
        )
        stored_hash = await connection.scalar(
            text(
                "SELECT content_hash FROM project_revisions WHERE id=:id"
            ),
            {"id": revision_id},
        )
        if stored_hash != canonical_sha256(manifest):
            raise LegacyImportCollision(
                f"revision content collision for {source_identity}"
            )
        if source_row is not None:
            checksum = _content_sha(source_row)
            replayed = await _mapping_status(
                connection,
                tenant_id=context.tenant_id,
                source_store="history",
                entity_type="model_snapshots",
                source_id=str(source_row["id"]),
                content_sha256=checksum,
                target_table="project_revisions",
                target_id=str(revision_id),
            )
            if not replayed:
                await connection.execute(
                    text(
                        """
                        INSERT INTO legacy_snapshot_mappings (
                            tenant_id, legacy_snapshot_id, panel_id,
                            project_id, revision_id, legacy_version,
                            source, prompt, status, created_at
                        ) VALUES (
                            :tenant, :snapshot, :panel,
                            :project, :revision, :version,
                            :source, :prompt, :status, :created_at
                        )
                        ON CONFLICT (
                            tenant_id, legacy_snapshot_id
                        ) DO NOTHING
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "snapshot": str(source_row["id"]),
                        "panel": panel_id,
                        "project": project_id,
                        "revision": revision_id,
                        "version": int(source_row["version"]),
                        "source": source_row.get("source") or "legacy",
                        "prompt": source_row.get("prompt") or "",
                        "status": source_row.get("status") or "unknown",
                        "created_at": _dt(source_row.get("created_at")),
                    },
                )
                await _record_mapping(
                    connection,
                    state,
                    context,
                    source_store="history",
                    entity_type="model_snapshots",
                    source_id=str(source_row["id"]),
                    target_table="project_revisions",
                    target_id=revision_id,
                    content_sha256=checksum,
                )
            result = manifest["result"]
            request_id = str(result.get("request_id") or "").strip()
            if request_id:
                state.requests[request_id] = (
                    context,
                    project_id,
                    revision_id,
                )
            state.counted("project_revisions")
            state.counted("legacy_snapshot_mappings")
        previous = revision_id

    await connection.execute(
        text(
            """
            UPDATE project_branches
            SET head_revision_id=:head,
                next_revision_number=:next_number,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=:branch
            """
        ),
        {
            "head": revision_ids[-1],
            "next_number": len(revision_ids) + 1,
            "branch": branch_id,
        },
    )


async def _import_history(state: _ImportState) -> None:
    rows = state.snapshot.rows["history"]
    imported_users = {
        str(row["id"]) for row in state.snapshot.rows["auth"]["users"]
    }
    sessions = {str(row["id"]): row for row in rows["sessions"]}
    panels_by_session: dict[str, list[dict[str, Any]]] = {}
    for panel in rows["panels"]:
        panels_by_session.setdefault(str(panel["session_id"]), []).append(panel)
    messages_by_panel: dict[str, list[dict[str, Any]]] = {}
    for message in rows["messages"]:
        messages_by_panel.setdefault(str(message["panel_id"]), []).append(message)
    snapshots_by_panel: dict[str, list[dict[str, Any]]] = {}
    for snapshot in rows["model_snapshots"]:
        snapshots_by_panel.setdefault(str(snapshot["panel_id"]), []).append(snapshot)

    for session_id, session in sorted(sessions.items()):
        context = _session_context(session, imported_users)
        if context.quarantined:
            state.quarantined_sources.add(f"history.sessions:{session_id}")
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
            role="migrator",
        ) as connection:
            await _ensure_context(
                connection,
                context,
                display_name="导入的历史项目",
            )
            project_id = await _ensure_project(
                connection,
                context,
                source_store="history-session",
                source_id=session_id,
                name=str(session.get("title") or "导入的 CAD 项目"),
                created_at=_dt(session.get("created_at")),
            )
            checksum = _content_sha(session)
            if not await _mapping_status(
                connection,
                tenant_id=context.tenant_id,
                source_store="history",
                entity_type="sessions",
                source_id=session_id,
                content_sha256=checksum,
                target_table="workspace_sessions",
                target_id=session_id,
            ):
                await connection.execute(
                    text(
                        """
                        INSERT INTO workspace_sessions (
                            id, tenant_id, project_id,
                            created_by_principal_id, legacy_user_id,
                            title, created_at, updated_at
                        ) VALUES (
                            :id, :tenant, :project, :principal, :user_id,
                            :title, :created_at, :updated_at
                        )
                        ON CONFLICT (tenant_id, id) DO NOTHING
                        """
                    ),
                    {
                        "id": session_id,
                        "tenant": context.tenant_id,
                        "project": project_id,
                        "principal": context.principal_id,
                        "user_id": session.get("user_id"),
                        "title": session.get("title") or "",
                        "created_at": _dt(session.get("created_at")),
                        "updated_at": _dt(session.get("updated_at")),
                    },
                )
                await _record_mapping(
                    connection,
                    state,
                    context,
                    source_store="history",
                    entity_type="sessions",
                    source_id=session_id,
                    target_table="workspace_sessions",
                    target_id=session_id,
                    content_sha256=checksum,
                )
            await _record_mapping(
                connection,
                state,
                context,
                source_store="history",
                entity_type="projects",
                source_id=session_id,
                target_table="projects",
                target_id=project_id,
                content_sha256=checksum,
            )
            state.sessions[session_id] = (context, project_id)
            state.remember(context)
            state.counted("projects")
            state.counted("workspace_sessions")

            for panel in sorted(
                panels_by_session.get(session_id, []),
                key=lambda item: str(item["id"]),
            ):
                panel_id = str(panel["id"])
                checksum = _content_sha(panel)
                replayed = await _mapping_status(
                    connection,
                    tenant_id=context.tenant_id,
                    source_store="history",
                    entity_type="panels",
                    source_id=panel_id,
                    content_sha256=checksum,
                    target_table="workspace_panels",
                    target_id=panel_id,
                )
                if not replayed:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO workspace_panels (
                                id, tenant_id, session_id, title,
                                current_code, current_params, created_at
                            ) VALUES (
                                :id, :tenant, :session, :title,
                                :code, CAST(:params AS jsonb), :created_at
                            )
                            ON CONFLICT (tenant_id, id) DO NOTHING
                            """
                        ),
                        {
                            "id": panel_id,
                            "tenant": context.tenant_id,
                            "session": session_id,
                            "title": panel.get("title") or "",
                            "code": panel.get("current_code"),
                            "params": _dump_json(
                                _load_json(panel.get("current_params"))
                            )
                            if panel.get("current_params")
                            else None,
                            "created_at": _dt(panel.get("created_at")),
                        },
                    )
                    await _record_mapping(
                        connection,
                        state,
                        context,
                        source_store="history",
                        entity_type="panels",
                        source_id=panel_id,
                        target_table="workspace_panels",
                        target_id=panel_id,
                        content_sha256=checksum,
                    )
                state.panels[panel_id] = (context, project_id)
                state.counted("workspace_panels")

                for message in sorted(
                    messages_by_panel.get(panel_id, []),
                    key=lambda item: int(item["id"]),
                ):
                    source_id = str(message["id"])
                    checksum = _content_sha(message)
                    if not await _mapping_status(
                        connection,
                        tenant_id=context.tenant_id,
                        source_store="history",
                        entity_type="messages",
                        source_id=source_id,
                        content_sha256=checksum,
                        target_table="workspace_messages",
                        target_id=None,
                    ):
                        message_id = await connection.scalar(
                            text(
                                """
                                INSERT INTO workspace_messages (
                                    tenant_id, panel_id, role, content,
                                    result, created_at
                                ) VALUES (
                                    :tenant, :panel, :role, :content,
                                    CAST(:result AS jsonb), :created_at
                                )
                                RETURNING id
                                """
                            ),
                            {
                                "tenant": context.tenant_id,
                                "panel": panel_id,
                                "role": message["role"],
                                "content": message["content"],
                                "result": (
                                    _dump_json(_load_json(message["result"]))
                                    if message.get("result")
                                    else None
                                ),
                                "created_at": _dt(message.get("created_at")),
                            },
                        )
                        await _record_mapping(
                            connection,
                            state,
                            context,
                            source_store="history",
                            entity_type="messages",
                            source_id=source_id,
                            target_table="workspace_messages",
                            target_id=message_id,
                            content_sha256=checksum,
                        )
                    state.counted("workspace_messages")

                await _insert_project_revision_history(
                    connection,
                    state,
                    context,
                    project_id=project_id,
                    panel=panel,
                    snapshots=snapshots_by_panel.get(panel_id, []),
                )

    known_panels = set(state.panels)
    orphan_snapshots = [
        row
        for row in rows["model_snapshots"]
        if str(row["panel_id"]) not in known_panels
    ]
    orphan_by_panel: dict[str, list[dict[str, Any]]] = {}
    for row in orphan_snapshots:
        orphan_by_panel.setdefault(str(row["panel_id"]), []).append(row)
    for panel_id, snapshots in sorted(orphan_by_panel.items()):
        context = quarantine_principal(f"orphan-snapshot-panel:{panel_id}")
        project_id = await _orphan_project(
            state,
            context,
            store="orphan-snapshots",
            owner=panel_id,
        )
        session_id = f"orphan-snapshots:{panel_id}"
        synthetic_checksum = _content_sha(
            {
                "panel_id": panel_id,
                "snapshot_ids": sorted(str(row["id"]) for row in snapshots),
            }
        )
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
            role="migrator",
        ) as connection:
            await _ensure_context(
                connection,
                context,
                display_name="待认领的历史模型",
            )
            if not await _mapping_status(
                connection,
                tenant_id=context.tenant_id,
                source_store="history",
                entity_type="orphan_snapshot_sessions",
                source_id=panel_id,
                content_sha256=synthetic_checksum,
                target_table="workspace_sessions",
                target_id=session_id,
            ):
                created_at = min(_dt(row.get("created_at")) for row in snapshots)
                await connection.execute(
                    text(
                        """
                        INSERT INTO workspace_sessions (
                            id, tenant_id, project_id,
                            created_by_principal_id, title,
                            created_at, updated_at
                        ) VALUES (
                            :id, :tenant, :project, :principal,
                            '待认领的历史模型', :created, :created
                        )
                        """
                    ),
                    {
                        "id": session_id,
                        "tenant": context.tenant_id,
                        "project": project_id,
                        "principal": context.principal_id,
                        "created": created_at,
                    },
                )
                await _record_mapping(
                    connection,
                    state,
                    context,
                    source_store="history",
                    entity_type="orphan_snapshot_sessions",
                    source_id=panel_id,
                    target_table="workspace_sessions",
                    target_id=session_id,
                    content_sha256=synthetic_checksum,
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO workspace_panels (
                            id, tenant_id, session_id, title,
                            current_code, current_params, created_at
                        ) VALUES (
                            :id, :tenant, :session,
                            '待认领的历史模型', :code,
                            CAST(:params AS jsonb), :created
                        )
                        """
                    ),
                    {
                        "id": panel_id,
                        "tenant": context.tenant_id,
                        "session": session_id,
                        "code": snapshots[-1].get("code") or "",
                        "params": (
                            _dump_json(_load_json(snapshots[-1].get("params")))
                            if snapshots[-1].get("params")
                            else None
                        ),
                        "created": created_at,
                    },
                )
                await _record_mapping(
                    connection,
                    state,
                    context,
                    source_store="history",
                    entity_type="orphan_snapshot_panels",
                    source_id=panel_id,
                    target_table="workspace_panels",
                    target_id=panel_id,
                    content_sha256=synthetic_checksum,
                )
            await _insert_project_revision_history(
                connection,
                state,
                context,
                project_id=project_id,
                panel={"id": panel_id},
                snapshots=snapshots,
            )
        state.sessions[session_id] = (context, project_id)
        state.panels[panel_id] = (context, project_id)
        state.counted("projects")
        state.counted("workspace_sessions")
        state.counted("workspace_panels")
        state.quarantined_sources.update(
            f"history.model_snapshots:{row['id']}"
            for row in snapshots
        )


def _scope_session(scope_key: Any) -> str | None:
    value = str(scope_key or "")
    if not value.startswith("ws:"):
        return None
    remainder = value[3:]
    if ":" not in remainder:
        return None
    return remainder.rsplit(":", 1)[0] or None


async def _orphan_project(
    state: _ImportState,
    context: PrincipalContext,
    *,
    store: str,
    owner: str,
) -> UUID:
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
        role="migrator",
    ) as connection:
        await _ensure_context(
            connection,
            context,
            display_name=f"待认领 {store} 数据",
        )
        project_id = await _ensure_project(
            connection,
            context,
            source_store=store,
            source_id=owner,
            name=f"待认领的 {store} 项目",
        )
        checksum = _content_sha(
            {
                "store": store,
                "owner": owner,
                "quarantined": True,
            }
        )
        if not await _mapping_status(
            connection,
            tenant_id=context.tenant_id,
            source_store="orphan-projects",
            entity_type=store,
            source_id=owner,
            content_sha256=checksum,
            target_table="projects",
            target_id=str(project_id),
        ):
            await _record_mapping(
                connection,
                state,
                context,
                source_store="orphan-projects",
                entity_type=store,
                source_id=owner,
                target_table="projects",
                target_id=project_id,
                content_sha256=checksum,
            )
    state.remember(context)
    return project_id


async def _import_local_workflows(state: _ImportState) -> None:
    rows = state.snapshot.rows["history"]
    events_by_run: dict[str, list[dict[str, Any]]] = {}
    for event in rows["local_workflow_events"]:
        events_by_run.setdefault(str(event["workflow_run_id"]), []).append(event)
    status_map = {
        "PENDING": "failed",
        "RUNNING": "failed",
        "INTERRUPTED": "failed",
        "RECONCILING": "failed",
        "CANCEL_REQUESTED": "cancelled",
        "COMPLETED": "succeeded",
        "FAILED": "failed",
        "CANCELLED": "cancelled",
    }
    for row in sorted(rows["local_workflow_runs"], key=lambda item: str(item["id"])):
        source_id = str(row["id"])
        session_id = _scope_session(row.get("scope_key"))
        if session_id and session_id in state.sessions:
            context, project_id = state.sessions[session_id]
        else:
            context = quarantine_principal(
                f"local-workflow:{row.get('owner_id') or 'ownerless'}"
            )
            project_id = await _orphan_project(
                state,
                context,
                store="legacy-workflows",
                owner=str(row.get("owner_id") or "ownerless"),
            )
            state.quarantined_sources.add(
                f"history.local_workflow_runs:{source_id}"
            )
        workflow_id = _stable_uuid("workflow", context.tenant_id, source_id)
        source_events = sorted(
            events_by_run.get(source_id, []),
            key=lambda item: int(item["id"]),
        )
        result_payload = _load_json(row.get("result_json"))
        checksum = _content_sha(
            {
                "run": row,
                "events": source_events,
            }
        )
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
            role="migrator",
        ) as connection:
            await _ensure_context(
                connection,
                context,
                display_name="导入的任务主体",
            )
            if await _mapping_status(
                connection,
                tenant_id=context.tenant_id,
                source_store="history",
                entity_type="local_workflow_runs",
                source_id=source_id,
                content_sha256=checksum,
                target_table="workflow_runs",
                target_id=str(workflow_id),
            ):
                state.counted("workflow_runs")
                state.counted(
                    "task_events",
                    len(source_events)
                    + (1 if result_payload is not None else 0),
                )
                continue
            request_payload = _load_json(row.get("request_json"), {})
            status = status_map.get(str(row.get("state")), "failed")
            event_count = len(source_events) + (1 if result_payload is not None else 0)
            await connection.execute(
                text(
                    """
                    INSERT INTO workflow_runs (
                        id, tenant_id, project_id,
                        requested_by_principal_id, kind, status,
                        idempotency_key, request_payload_hash,
                        request_payload, last_event_sequence,
                        cancellation_requested_at, error_code,
                        error_message, created_at, started_at,
                        updated_at, completed_at
                    ) VALUES (
                        :id, :tenant, :project, :principal, :kind, :status,
                        :idempotency, :payload_hash,
                        CAST(:payload AS jsonb), :last_sequence,
                        :cancel_requested, :error_code,
                        :error_message, :created_at, :started_at,
                        :updated_at, :completed_at
                    )
                    ON CONFLICT (id) DO NOTHING
                    """
                ),
                {
                    "id": workflow_id,
                    "tenant": context.tenant_id,
                    "project": project_id,
                    "principal": context.principal_id,
                    "kind": row.get("kind") or "legacy",
                    "status": status,
                    "idempotency": f"legacy-local-run:{source_id}",
                    "payload_hash": canonical_sha256(request_payload),
                    "payload": _dump_json(request_payload),
                    "last_sequence": event_count,
                    "cancel_requested": (
                        _dt(row.get("updated_at"))
                        if row.get("cancel_requested")
                        else None
                    ),
                    "error_code": row.get("error_code"),
                    "error_message": row.get("error_message"),
                    "created_at": _dt(row.get("created_at")),
                    "started_at": (
                        _dt(row["started_at"])
                        if row.get("started_at")
                        else None
                    ),
                    "updated_at": _dt(row.get("updated_at")),
                    "completed_at": (
                        _dt(row["finished_at"])
                        if row.get("finished_at")
                        else _dt(row.get("updated_at"))
                    ),
                },
            )
            sequence = 0
            for event in source_events:
                sequence += 1
                payload = {
                    "legacy_event_id": event["id"],
                    "state": event.get("state"),
                    "payload": _load_json(event.get("payload_json")),
                }
                await connection.execute(
                    text(
                        """
                        INSERT INTO task_events (
                            id, tenant_id, workflow_run_id, sequence,
                            event_type, payload, occurred_at
                        ) VALUES (
                            :id, :tenant, :workflow, :sequence,
                            :event_type, CAST(:payload AS jsonb), :occurred_at
                        )
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {
                        "id": _stable_uuid(
                            "workflow-event",
                            context.tenant_id,
                            source_id,
                            event["id"],
                        ),
                        "tenant": context.tenant_id,
                        "workflow": workflow_id,
                        "sequence": sequence,
                        "event_type": f"legacy.{event['event_type']}",
                        "payload": _dump_json(payload),
                        "occurred_at": _dt(event.get("created_at")),
                    },
                )
            if result_payload is not None:
                sequence += 1
                await connection.execute(
                    text(
                        """
                        INSERT INTO task_events (
                            id, tenant_id, workflow_run_id, sequence,
                            event_type, payload, occurred_at
                        ) VALUES (
                            :id, :tenant, :workflow, :sequence,
                            'legacy.result', CAST(:payload AS jsonb), :occurred_at
                        )
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {
                        "id": _stable_uuid(
                            "workflow-result",
                            context.tenant_id,
                            source_id,
                        ),
                        "tenant": context.tenant_id,
                        "workflow": workflow_id,
                        "sequence": sequence,
                        "payload": _dump_json(result_payload),
                        "occurred_at": _dt(row.get("updated_at")),
                    },
                )
                request_id = str(result_payload.get("request_id") or "").strip()
                if request_id:
                    state.requests.setdefault(
                        request_id,
                        (context, project_id, None),
                    )
            await _record_mapping(
                connection,
                state,
                context,
                source_store="history",
                entity_type="local_workflow_runs",
                source_id=source_id,
                target_table="workflow_runs",
                target_id=workflow_id,
                content_sha256=checksum,
            )
            state.counted("workflow_runs")
            state.counted("task_events", sequence)


async def _import_dfm_and_knowledge(state: _ImportState) -> None:
    context = quarantine_principal("legacy-platform-engineering-knowledge")
    state.quarantined_sources.add("dfm:ownerless")
    state.quarantined_sources.add("knowledge:ownerless")
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
        role="migrator",
    ) as connection:
        await _ensure_context(
            connection,
            context,
            display_name="待认领的工程知识",
        )
        await _ensure_import_run(connection, state, context)
        dfm = state.snapshot.rows["dfm"]
        for row in sorted(dfm["rule_sets"], key=lambda item: str(item["id"])):
            source_id = str(row["id"])
            checksum = _content_sha(row)
            if not await _mapping_status(
                connection,
                tenant_id=context.tenant_id,
                source_store="dfm",
                entity_type="rule_sets",
                source_id=source_id,
                content_sha256=checksum,
                target_table="dfm_rule_sets",
                target_id=source_id,
            ):
                await connection.execute(
                    text(
                        """
                        INSERT INTO dfm_rule_sets (
                            tenant_id, id, name, process, version,
                            is_builtin, created_at
                        ) VALUES (
                            :tenant, :id, :name, :process, 'legacy',
                            :builtin, :created_at
                        )
                        ON CONFLICT (tenant_id, id) DO NOTHING
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "id": source_id,
                        "name": row["name"],
                        "process": row["process"],
                        "builtin": source_id.startswith("default_"),
                        "created_at": _dt(row.get("created_at")),
                    },
                )
                await _record_mapping(
                    connection,
                    state,
                    context,
                    source_store="dfm",
                    entity_type="rule_sets",
                    source_id=source_id,
                    target_table="dfm_rule_sets",
                    target_id=source_id,
                    content_sha256=checksum,
                )
            state.counted("dfm_rule_sets")
        for row in sorted(dfm["rules"], key=lambda item: str(item["id"])):
            source_id = str(row["id"])
            checksum = _content_sha(row)
            if not await _mapping_status(
                connection,
                tenant_id=context.tenant_id,
                source_store="dfm",
                entity_type="rules",
                source_id=source_id,
                content_sha256=checksum,
                target_table="dfm_rules",
                target_id=source_id,
            ):
                await connection.execute(
                    text(
                        """
                        INSERT INTO dfm_rules (
                            tenant_id, id, rule_set_id, process,
                            category, check_type, threshold_min,
                            threshold_max, unit, severity, description,
                            suggestion_template, enabled
                        ) VALUES (
                            :tenant, :id, :set_id, :process,
                            :category, :check_type, :threshold_min,
                            :threshold_max, :unit, :severity, :description,
                            :suggestion, :enabled
                        )
                        ON CONFLICT (tenant_id, id) DO NOTHING
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "id": source_id,
                        "set_id": row["rule_set_id"],
                        "process": row["process"],
                        "category": row["category"],
                        "check_type": row.get("check_type") or "geometric",
                        "threshold_min": row.get("threshold_min"),
                        "threshold_max": row.get("threshold_max"),
                        "unit": row.get("unit") or "mm",
                        "severity": row.get("severity") or "warning",
                        "description": row.get("description") or "",
                        "suggestion": row.get("suggestion_template") or "",
                        "enabled": bool(row.get("enabled", 1)),
                    },
                )
                await _record_mapping(
                    connection,
                    state,
                    context,
                    source_store="dfm",
                    entity_type="rules",
                    source_id=source_id,
                    target_table="dfm_rules",
                    target_id=source_id,
                    content_sha256=checksum,
                )
            state.counted("dfm_rules")

        knowledge = state.snapshot.rows["knowledge"]
        for row in sorted(
            knowledge["kg_nodes"],
            key=lambda item: (str(item.get("customer_id")), str(item["id"])),
        ):
            source_id = f"{row.get('customer_id') or 'default'}:{row['id']}"
            checksum = _content_sha(row)
            if not await _mapping_status(
                connection,
                tenant_id=context.tenant_id,
                source_store="knowledge",
                entity_type="kg_nodes",
                source_id=source_id,
                content_sha256=checksum,
                target_table="knowledge_nodes",
                target_id=source_id,
            ):
                await connection.execute(
                    text(
                        """
                        INSERT INTO knowledge_nodes (
                            tenant_id, customer_id, id, type, name,
                            properties, source
                        ) VALUES (
                            :tenant, :customer, :id, :type, :name,
                            CAST(:properties AS jsonb), 'legacy'
                        )
                        ON CONFLICT (tenant_id, customer_id, id) DO NOTHING
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "customer": row.get("customer_id") or "default",
                        "id": row["id"],
                        "type": row["type"],
                        "name": row["name"],
                        "properties": _dump_json(
                            _load_json(row.get("properties"), {})
                        ),
                    },
                )
                await _record_mapping(
                    connection,
                    state,
                    context,
                    source_store="knowledge",
                    entity_type="kg_nodes",
                    source_id=source_id,
                    target_table="knowledge_nodes",
                    target_id=source_id,
                    content_sha256=checksum,
                )
            state.counted("knowledge_nodes")
        for row in sorted(
            knowledge["kg_edges"],
            key=lambda item: int(item["id"]),
        ):
            source_id = str(row["id"])
            checksum = _content_sha(row)
            if not await _mapping_status(
                connection,
                tenant_id=context.tenant_id,
                source_store="knowledge",
                entity_type="kg_edges",
                source_id=source_id,
                content_sha256=checksum,
                target_table="knowledge_edges",
                target_id=None,
            ):
                edge_id = await connection.scalar(
                    text(
                        """
                        INSERT INTO knowledge_edges (
                            tenant_id, customer_id, legacy_id,
                            source_id, target_id, relationship,
                            properties, source
                        ) VALUES (
                            :tenant, :customer, :legacy_id,
                            :source_id, :target_id, :relationship,
                            CAST(:properties AS jsonb), 'legacy'
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "customer": row.get("customer_id") or "default",
                        "legacy_id": row["id"],
                        "source_id": row["source_id"],
                        "target_id": row["target_id"],
                        "relationship": row["relationship"],
                        "properties": _dump_json(
                            _load_json(row.get("properties"), {})
                        ),
                    },
                )
                await _record_mapping(
                    connection,
                    state,
                    context,
                    source_store="knowledge",
                    entity_type="kg_edges",
                    source_id=source_id,
                    target_table="knowledge_edges",
                    target_id=edge_id,
                    content_sha256=checksum,
                )
            state.counted("knowledge_edges")


async def _request_owner(
    state: _ImportState,
    request_id: str,
    *,
    source: str,
) -> tuple[PrincipalContext, UUID, UUID | None]:
    known = state.requests.get(request_id)
    if known:
        return known
    context = quarantine_principal(f"{source}-request:{request_id}")
    project_id = await _orphan_project(
        state,
        context,
        store=source,
        owner=request_id,
    )
    state.quarantined_sources.add(f"{source}:{request_id}")
    result = (context, project_id, None)
    state.requests[request_id] = result
    return result


async def _import_feedback_and_onshape(state: _ImportState) -> None:
    rows = state.snapshot.rows["history"]
    for row in sorted(rows["feedback"], key=lambda item: int(item["id"])):
        request_id = str(row["request_id"])
        context, project_id, _ = await _request_owner(
            state,
            request_id,
            source="feedback",
        )
        source_id = str(row["id"])
        target_id = _stable_uuid(
            "feedback",
            context.tenant_id,
            source_id,
        )
        checksum = _content_sha(row)
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
            role="migrator",
        ) as connection:
            if not await _mapping_status(
                connection,
                tenant_id=context.tenant_id,
                source_store="history",
                entity_type="feedback",
                source_id=source_id,
                content_sha256=checksum,
                target_table="product_feedback",
                target_id=str(target_id),
            ):
                await connection.execute(
                    text(
                        """
                        INSERT INTO product_feedback (
                            id, tenant_id, principal_id, project_id,
                            request_id, rating, printed, note, created_at
                        ) VALUES (
                            :id, :tenant, :principal, :project,
                            :request, :rating, :printed, :note, :created
                        )
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {
                        "id": target_id,
                        "tenant": context.tenant_id,
                        "principal": context.principal_id,
                        "project": project_id,
                        "request": request_id,
                        "rating": row.get("rating"),
                        "printed": row.get("printed"),
                        "note": row.get("note"),
                        "created": _dt(row.get("created_at")),
                    },
                )
                await _record_mapping(
                    connection,
                    state,
                    context,
                    source_store="history",
                    entity_type="feedback",
                    source_id=source_id,
                    target_table="product_feedback",
                    target_id=target_id,
                    content_sha256=checksum,
                )
            state.counted("product_feedback")

    imported_users = {
        str(row["id"]) for row in state.snapshot.rows["auth"]["users"]
    }
    for row in sorted(rows["onshape_links"], key=lambda item: int(item["id"])):
        request_id = str(row["request_id"])
        user_id = str(row.get("user_id") or "")
        if user_id and user_id in imported_users:
            context = user_principal(user_id)
            known = state.requests.get(request_id)
            if known and known[0].tenant_id == context.tenant_id:
                project_id = known[1]
            else:
                project_id = await _orphan_project(
                    state,
                    context,
                    store="onshape",
                    owner=request_id,
                )
        else:
            context, project_id, _ = await _request_owner(
                state,
                request_id,
                source="onshape",
            )
        source_id = str(row["id"])
        checksum = _content_sha(row)
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
            role="migrator",
        ) as connection:
            if not await _mapping_status(
                connection,
                tenant_id=context.tenant_id,
                source_store="history",
                entity_type="onshape_links",
                source_id=source_id,
                content_sha256=checksum,
                target_table="connector_links",
                target_id=None,
            ):
                target_id = await connection.scalar(
                    text(
                        """
                        INSERT INTO connector_links (
                            tenant_id, principal_id, project_id, connector,
                            request_id, document_id, workspace_id, element_id,
                            translation_id, status, external_url,
                            document_name, source_filename, mode,
                            raw_response, created_at, updated_at
                        ) VALUES (
                            :tenant, :principal, :project, 'onshape',
                            :request, :document, :workspace, :element,
                            :translation, :status, :url,
                            :document_name, :filename, :mode,
                            CAST(:raw AS jsonb), :created, :updated
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "principal": context.principal_id,
                        "project": project_id,
                        "request": request_id,
                        "document": row["document_id"],
                        "workspace": row["workspace_id"],
                        "element": row.get("element_id"),
                        "translation": row.get("translation_id"),
                        "status": row["status"],
                        "url": row["onshape_url"],
                        "document_name": row.get("document_name") or "",
                        "filename": row.get("step_filename") or "",
                        "mode": row.get("mode") or "import_step",
                        "raw": (
                            _dump_json(_load_json(row["raw_response"]))
                            if row.get("raw_response")
                            else None
                        ),
                        "created": _dt(row.get("created_at")),
                        "updated": _dt(row.get("updated_at")),
                    },
                )
                await _record_mapping(
                    connection,
                    state,
                    context,
                    source_store="history",
                    entity_type="onshape_links",
                    source_id=source_id,
                    target_table="connector_links",
                    target_id=target_id,
                    content_sha256=checksum,
                )
            state.counted("connector_links")


def _media_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    explicit = {
        ".step": "application/step",
        ".stp": "application/step",
        ".stl": "application/sla",
        ".dxf": "application/dxf",
        ".f3d": "application/vnd.autodesk.fusion360",
    }
    return explicit.get(
        suffix,
        mimetypes.guess_type(filename)[0] or "application/octet-stream",
    )


async def _object_exists_with_checksum(item: LegacyFile, key: str) -> bool:
    try:
        head = await head_object(key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    if head["size_bytes"] != item.size_bytes:
        raise LegacyImportCollision(
            f"object key {key} already exists with a different size"
        )
    evidence = await sha256_object(key)
    if evidence["sha256"] != item.sha256:
        raise LegacyImportCollision(
            f"object key {key} already exists with a different checksum"
        )
    return True


def _request_from_generated_path(relative_path: str) -> str | None:
    parts = Path(relative_path).parts
    if len(parts) != 2:
        return None
    if parts[0] in {"capability_users", "fusion360"}:
        return None
    return parts[0]


async def _import_file_metadata(
    state: _ImportState,
    item: LegacyFile,
    *,
    context: PrincipalContext,
    project_id: UUID | None,
    revision_id: UUID | None,
    request_id: str,
    filename: str,
    target_table: str,
    source_store: str,
    scope: str | None = None,
) -> None:
    target_id = _stable_uuid(
        "file",
        context.tenant_id,
        source_store,
        item.relative_path,
        item.sha256,
    )
    object_key = (
        f"legacy/tenants/{context.tenant_id}/{source_store}/"
        f"{request_id}/{item.sha256}/{filename}"
    )
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
        role="migrator",
    ) as connection:
        replayed = await _mapping_status(
            connection,
            tenant_id=context.tenant_id,
            source_store=source_store,
            entity_type="files",
            source_id=item.relative_path,
            content_sha256=item.sha256,
            target_table=target_table,
            target_id=str(target_id),
        )
    if replayed:
        if not await _object_exists_with_checksum(item, object_key):
            raise LegacyImportCollision(
                f"mapped object {object_key} is missing"
            )
    else:
        if not await _object_exists_with_checksum(item, object_key):
            uploaded = await put_file(
                object_key,
                item.path,
                content_type=_media_type(filename),
            )
            if (
                uploaded["sha256"] != item.sha256
                or uploaded["size_bytes"] != item.size_bytes
            ):
                raise LegacyImportError(
                    f"uploaded object verification failed for {item.relative_path}"
                )
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
            role="migrator",
        ) as connection:
            if target_table == "project_files":
                if project_id is None:
                    raise LegacyImportError(
                        "project file import requires a project"
                    )
                await connection.execute(
                    text(
                        """
                        INSERT INTO project_files (
                            id, tenant_id, project_id, revision_id,
                            request_id, filename, content_type,
                            size_bytes, sha256, object_key, source
                        ) VALUES (
                            :id, :tenant, :project, :revision,
                            :request, :filename, :content_type,
                            :size, :sha, :key, 'legacy'
                        )
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {
                        "id": target_id,
                        "tenant": context.tenant_id,
                        "project": project_id,
                        "revision": revision_id,
                        "request": request_id,
                        "filename": filename,
                        "content_type": _media_type(filename),
                        "size": item.size_bytes,
                        "sha": item.sha256,
                        "key": object_key,
                    },
                )
            elif target_table == "connector_artifacts":
                await connection.execute(
                    text(
                        """
                        INSERT INTO connector_artifacts (
                            id, tenant_id, principal_id, connector,
                            request_id, filename, content_type,
                            size_bytes, sha256, object_key
                        ) VALUES (
                            :id, :tenant, :principal, 'fusion360',
                            :request, :filename, :content_type,
                            :size, :sha, :key
                        )
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {
                        "id": target_id,
                        "tenant": context.tenant_id,
                        "principal": context.principal_id,
                        "request": request_id,
                        "filename": filename,
                        "content_type": _media_type(filename),
                        "size": item.size_bytes,
                        "sha": item.sha256,
                        "key": object_key,
                    },
                )
            elif target_table == "capability_artifacts":
                await connection.execute(
                    text(
                        """
                        INSERT INTO capability_artifacts (
                            id, tenant_id, principal_id, project_id,
                            scope, request_id, filename, content_type,
                            size_bytes, sha256, object_key
                        ) VALUES (
                            :id, :tenant, :principal, :project,
                            :scope, :request, :filename, :content_type,
                            :size, :sha, :key
                        )
                        ON CONFLICT (id) DO NOTHING
                        """
                    ),
                    {
                        "id": target_id,
                        "tenant": context.tenant_id,
                        "principal": context.principal_id,
                        "project": project_id,
                        "scope": scope,
                        "request": request_id,
                        "filename": filename,
                        "content_type": _media_type(filename),
                        "size": item.size_bytes,
                        "sha": item.sha256,
                        "key": object_key,
                    },
                )
            else:
                raise LegacyImportError(
                    f"unsupported file target table {target_table}"
                )
            await _record_mapping(
                connection,
                state,
                context,
                source_store=source_store,
                entity_type="files",
                source_id=item.relative_path,
                target_table=target_table,
                target_id=target_id,
                content_sha256=item.sha256,
            )
    state.object_count += 1
    state.object_bytes += item.size_bytes
    state.counted(target_table)


async def _import_files(state: _ImportState) -> None:
    for item in state.snapshot.files:
        parts = Path(item.relative_path).parts
        if item.store == "generated_files":
            request_id = _request_from_generated_path(item.relative_path)
            if request_id is None:
                continue
            context, project_id, revision_id = await _request_owner(
                state,
                request_id,
                source="generated-files",
            )
            await _import_file_metadata(
                state,
                item,
                context=context,
                project_id=project_id,
                revision_id=revision_id,
                request_id=request_id,
                filename=parts[-1],
                target_table="project_files",
                source_store="generated_files",
            )
        elif item.store in {
            "fusion_artifacts",
            "fusion_agent_artifacts",
        }:
            if len(parts) < 2:
                continue
            request_id = parts[-2]
            owner_reference = parts[0]
            context = quarantine_principal(
                f"fusion-artifact-owner:{owner_reference}"
            )
            state.quarantined_sources.add(
                f"{item.store}.files:{item.relative_path}"
            )
            await _import_file_metadata(
                state,
                item,
                context=context,
                project_id=None,
                revision_id=None,
                request_id=request_id,
                filename=parts[-1],
                target_table="connector_artifacts",
                source_store=item.store,
            )
        elif item.store == "capability_artifacts":
            if len(parts) < 4:
                continue
            owner_reference, scope, request_id = parts[:3]
            if scope not in {"uploads", "runs"}:
                continue
            context = quarantine_principal(
                f"capability-owner:{owner_reference}"
            )
            project_id = await _orphan_project(
                state,
                context,
                store="capability",
                owner=owner_reference,
            )
            state.quarantined_sources.add(
                f"capability_artifacts.files:{item.relative_path}"
            )
            await _import_file_metadata(
                state,
                item,
                context=context,
                project_id=project_id,
                revision_id=None,
                request_id=request_id,
                filename=parts[-1],
                target_table="capability_artifacts",
                source_store="capability_artifacts",
                scope=scope,
            )


def _fusion_context(owner_id: Any, source: str) -> PrincipalContext:
    return quarantine_principal(
        f"{source}-owner:{str(owner_id or 'ownerless')}"
    )


async def _import_fusion_records(state: _ImportState) -> None:
    tokens = state.snapshot.rows["fusion_tokens"]
    for row in tokens["oauth_tokens"]:
        owner_id = str(row["owner_id"])
        context = _fusion_context(owner_id, "fusion-token")
        checksum = _content_sha(row)
        state.quarantined_sources.add(f"fusion_tokens.oauth_tokens:{owner_id}")
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
            role="migrator",
        ) as connection:
            await _ensure_context(
                connection,
                context,
                display_name="待认领的 Fusion 凭据",
            )
            if not await _mapping_status(
                connection,
                tenant_id=context.tenant_id,
                source_store="fusion_tokens",
                entity_type="oauth_tokens",
                source_id=owner_id,
                content_sha256=checksum,
                target_table="connector_tokens",
                target_id=owner_id,
            ):
                await connection.execute(
                    text(
                        """
                        INSERT INTO connector_tokens (
                            tenant_id, principal_id, connector,
                            encrypted_token, encryption_key_id,
                            created_at, updated_at
                        ) VALUES (
                            :tenant, :principal, 'fusion360',
                            :token, 'legacy-fernet',
                            :updated, :updated
                        )
                        ON CONFLICT (
                            tenant_id, principal_id, connector
                        ) DO NOTHING
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "principal": context.principal_id,
                        "token": bytes(row["encrypted_token"]),
                        "updated": _dt(row.get("updated_at")),
                    },
                )
                await _record_mapping(
                    connection,
                    state,
                    context,
                    source_store="fusion_tokens",
                    entity_type="oauth_tokens",
                    source_id=owner_id,
                    target_table="connector_tokens",
                    target_id=owner_id,
                    content_sha256=checksum,
                )
            state.counted("connector_tokens")
    for row in tokens["oauth_states"]:
        owner_id = str(row["owner_id"])
        context = _fusion_context(owner_id, "fusion-token")
        source_id = str(row["state"])
        checksum = _content_sha(row)
        state.quarantined_sources.add(f"fusion_tokens.oauth_states:{source_id}")
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
            role="migrator",
        ) as connection:
            await _ensure_context(
                connection,
                context,
                display_name="待认领的 Fusion 凭据",
            )
            if not await _mapping_status(
                connection,
                tenant_id=context.tenant_id,
                source_store="fusion_tokens",
                entity_type="oauth_states",
                source_id=source_id,
                content_sha256=checksum,
                target_table="connector_oauth_states",
                target_id=source_id,
            ):
                await connection.execute(
                    text(
                        """
                        INSERT INTO connector_oauth_states (
                            tenant_id, principal_id, connector, state,
                            encrypted_verifier, expires_at,
                            consumed_at, created_at
                        ) VALUES (
                            :tenant, :principal, 'fusion360', :state,
                            :verifier, :expires, :consumed, :created
                        )
                        ON CONFLICT (tenant_id, connector, state) DO NOTHING
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "principal": context.principal_id,
                        "state": source_id,
                        "verifier": bytes(row["encrypted_verifier"]),
                        "expires": _dt(row["expires_at"]),
                        "consumed": (
                            _dt(row["consumed_at"])
                            if row.get("consumed_at")
                            else None
                        ),
                        "created": _dt(
                            row.get("expires_at"),
                        ),
                    },
                )
                await _record_mapping(
                    connection,
                    state,
                    context,
                    source_store="fusion_tokens",
                    entity_type="oauth_states",
                    source_id=source_id,
                    target_table="connector_oauth_states",
                    target_id=source_id,
                    content_sha256=checksum,
                )
            state.counted("connector_oauth_states")

    record_sources = (
        ("fusion_runtime", state.snapshot.rows["fusion_runtime"]),
        ("fusion_agent", state.snapshot.rows["fusion_agent"]),
    )
    for source_store, tables in record_sources:
        for table_name, source_rows in tables.items():
            for index, row in enumerate(source_rows):
                owner_id = row.get("owner_id")
                context = _fusion_context(owner_id, source_store)
                preferred_keys = {
                    "agent_reports": ("report_id", "request_id"),
                    "agent_artifacts": ("request_id", "filename"),
                    "agent_plans": ("request_id",),
                    "agent_connectors": ("connector_instance_id",),
                    "connectors": ("connector_instance_id",),
                    "tasks": ("request_id",),
                    "approvals": ("approval_id",),
                    "snapshots": ("snapshot_id",),
                }.get(table_name, ())
                source_parts = [
                    str(row[key])
                    for key in preferred_keys
                    if row.get(key) is not None
                ]
                source_id = ":".join(source_parts) or f"row-{index}"
                checksum = _content_sha(row)
                target_id = _stable_uuid(
                    "connector-record",
                    context.tenant_id,
                    source_store,
                    table_name,
                    source_id,
                )
                state.quarantined_sources.add(
                    f"{source_store}.{table_name}:{source_id}"
                )
                payload = {
                    key: _safe_for_fingerprint(value)
                    for key, value in row.items()
                }
                async with tenant_transaction(
                    context.tenant_id,
                    context.principal_id,
                    role="migrator",
                ) as connection:
                    await _ensure_context(
                        connection,
                        context,
                        display_name="待认领的 Fusion 数据",
                    )
                    if not await _mapping_status(
                        connection,
                        tenant_id=context.tenant_id,
                        source_store=source_store,
                        entity_type=table_name,
                        source_id=source_id,
                        content_sha256=checksum,
                        target_table="connector_records",
                        target_id=str(target_id),
                    ):
                        await connection.execute(
                            text(
                                """
                                INSERT INTO connector_records (
                                    id, tenant_id, principal_id, connector,
                                    record_type, external_id, state,
                                    payload, created_at, updated_at
                                ) VALUES (
                                    :id, :tenant, :principal, 'fusion360',
                                    :record_type, :external_id, :state,
                                    CAST(:payload AS jsonb), :created, :updated
                                )
                                ON CONFLICT (id) DO NOTHING
                                """
                            ),
                            {
                                "id": target_id,
                                "tenant": context.tenant_id,
                                "principal": context.principal_id,
                                "record_type": table_name,
                                "external_id": source_id,
                                "state": row.get("status"),
                                "payload": _dump_json(payload),
                                "created": _dt(
                                    row.get("created_at")
                                    or row.get("registered_at")
                                    or row.get("received_at")
                                    or row.get("last_seen_at")
                                ),
                                "updated": _dt(
                                    row.get("updated_at")
                                    or row.get("last_heartbeat")
                                    or row.get("received_at")
                                    or row.get("last_seen_at")
                                    or row.get("created_at")
                                ),
                            },
                        )
                        await _record_mapping(
                            connection,
                            state,
                            context,
                            source_store=source_store,
                            entity_type=table_name,
                            source_id=source_id,
                            target_table="connector_records",
                            target_id=target_id,
                            content_sha256=checksum,
                        )
                    state.counted("connector_records")


async def _finalize_runs(
    state: _ImportState,
    report: LegacyImportReport,
) -> None:
    report_json = _dump_json(report.as_dict())
    for context in state.tenant_contexts.values():
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
            role="migrator",
        ) as connection:
            await connection.execute(
                text(
                    """
                    UPDATE legacy_import_runs
                    SET status='succeeded', report=CAST(:report AS jsonb),
                        completed_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=:tenant
                      AND source_fingerprint=:fingerprint
                    """
                ),
                {
                    "report": report_json,
                    "tenant": context.tenant_id,
                    "fingerprint": state.snapshot.source_fingerprint,
                },
            )


async def _fail_runs(state: _ImportState, error: Exception) -> None:
    failure = _dump_json(
        {
            "status": "failed",
            "error_type": type(error).__name__,
        }
    )
    for context in state.tenant_contexts.values():
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
            role="migrator",
        ) as connection:
            await connection.execute(
                text(
                    """
                    UPDATE legacy_import_runs
                    SET status='failed', report=CAST(:report AS jsonb),
                        completed_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=:tenant
                      AND source_fingerprint=:fingerprint
                      AND status='running'
                    """
                ),
                {
                    "report": failure,
                    "tenant": context.tenant_id,
                    "fingerprint": state.snapshot.source_fingerprint,
                },
            )


async def _persist_quarantine_records(state: _ImportState) -> None:
    """Persist every ownership exception instead of leaving it as a report count."""
    if not state.quarantined_sources:
        return
    context = quarantine_principal("legacy-import-quarantine-index")
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
        role="migrator",
    ) as connection:
        await _ensure_context(
            connection,
            context,
            display_name="遗留数据隔离索引",
        )
        await _ensure_import_run(connection, state, context)
        for source_reference in sorted(state.quarantined_sources):
            record_id = _stable_uuid(
                "quarantine-record",
                state.snapshot.source_fingerprint,
                source_reference,
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO legacy_quarantine_records (
                        id, tenant_id, principal_id, source_fingerprint,
                        source_reference, reason, metadata
                    ) VALUES (
                        :id, :tenant, :principal, :fingerprint,
                        :reference, 'legacy_owner_unresolved',
                        CAST(:metadata AS jsonb)
                    )
                    ON CONFLICT (
                        tenant_id, source_fingerprint, source_reference
                    ) DO NOTHING
                    """
                ),
                {
                    "id": record_id,
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                    "fingerprint": state.snapshot.source_fingerprint,
                    "reference": source_reference,
                    "metadata": _dump_json(
                        {
                            "source_reference_sha256": hashlib.sha256(
                                source_reference.encode("utf-8")
                            ).hexdigest(),
                        }
                    ),
                },
            )
    state.remember(context)
    state.target_counts["legacy_quarantine_records"] = len(
        state.quarantined_sources
    )


async def import_legacy_data(
    paths: LegacySourcePaths,
) -> LegacyImportReport:
    snapshot = capture_legacy_snapshot(paths)
    state = _ImportState(snapshot=snapshot)
    try:
        await _import_auth(state)
        await _import_history(state)
        await _import_local_workflows(state)
        await _import_dfm_and_knowledge(state)
        await _import_feedback_and_onshape(state)
        await _import_fusion_records(state)
        await _import_files(state)
        await _persist_quarantine_records(state)
        report = LegacyImportReport(
            source_fingerprint=snapshot.source_fingerprint,
            source_counts=snapshot.source_counts,
            target_counts=dict(sorted(state.target_counts.items())),
            quarantined_source_count=len(state.quarantined_sources),
            object_count=state.object_count,
            object_bytes=state.object_bytes,
        )
        await _finalize_runs(state, report)
        return report
    except Exception as error:
        await _fail_runs(state, error)
        raise


async def rollback_legacy_import(source_fingerprint: str) -> dict[str, Any]:
    """Rollback an import before cutover.

    This operator path intentionally requires the migration-owner connection.
    It refuses projects that gained non-imported revisions or workflow rows.
    """
    object_keys: list[str] = []
    async with get_database_engine().begin() as connection:
        mappings = (
            await connection.execute(
                text(
                    """
                    SELECT tenant_id, target_table, target_id
                    FROM legacy_import_mappings
                    WHERE import_run_id IN (
                        SELECT id FROM legacy_import_runs
                        WHERE source_fingerprint=:fingerprint
                    )
                    """
                ),
                {"fingerprint": source_fingerprint},
            )
        ).mappings().all()
        if not mappings:
            return {
                "source_fingerprint": source_fingerprint,
                "status": "not_found",
                "deleted_objects": 0,
            }
        project_ids = {
            UUID(row["target_id"])
            for row in mappings
            if row["target_table"] == "projects"
        }
        mapped_workflows = {
            UUID(row["target_id"])
            for row in mappings
            if row["target_table"] == "workflow_runs"
        }
        if project_ids:
            # Cloud documents are created by the branch-head trigger even for
            # imported projects. Lock before checking for post-import activity.
            await connection.execute(
                text('SELECT id FROM projects WHERE id = ANY(:projects) FOR UPDATE'),
                {'projects':list(project_ids)},
            )
            unsafe_revision = await connection.scalar(
                text(
                    """
                    SELECT count(*) FROM project_revisions
                    WHERE project_id = ANY(:projects)
                      AND kind NOT IN ('initial', 'imported')
                    """
                ),
                {"projects": list(project_ids)},
            )
            unsafe_workflow = await connection.scalar(
                text(
                    """
                    SELECT count(*) FROM workflow_runs
                    WHERE project_id = ANY(:projects)
                      AND NOT (id = ANY(:workflows))
                    """
                ),
                {
                    "projects": list(project_ids),
                    "workflows": list(mapped_workflows) or [UUID(int=0)],
                },
            )
            if int(unsafe_revision or 0) or int(unsafe_workflow or 0):
                raise LegacyRollbackUnsafe(
                    "imported projects contain post-import product data"
                )
        for table in (
            "project_files",
            "connector_artifacts",
            "capability_artifacts",
        ):
            ids = [
                UUID(row["target_id"])
                for row in mappings
                if row["target_table"] == table
            ]
            if ids:
                object_keys.extend(
                    (
                        await connection.execute(
                            text(
                                f"SELECT object_key FROM {table} "
                                "WHERE id = ANY(:ids)"
                            ),
                            {"ids": ids},
                        )
                    ).scalars()
                )
        await connection.execute(
            text(
                "SELECT set_config('app.legacy_import_rollback', 'on', true)"
            )
        )
        grouped: dict[tuple[str, UUID], list[str]] = {}
        for row in mappings:
            grouped.setdefault(
                (str(row["target_table"]), row["tenant_id"]),
                [],
            ).append(str(row["target_id"]))

        uuid_tables = {
            "project_files",
            "product_feedback",
            "connector_records",
            "connector_audit_records",
            "connector_artifacts",
            "capability_artifacts",
            "workflow_runs",
        }
        bigint_tables = {
            "auth_verification_codes",
            "workspace_messages",
            "knowledge_edges",
            "connector_links",
        }
        text_id_tables = {
            "auth_users",
            "dfm_rules",
            "dfm_rule_sets",
        }
        tenant_text_id_tables = {
            "dfm_rules",
            "dfm_rule_sets",
            "knowledge_nodes",
        }
        for (table, tenant_id), target_ids in sorted(
            grouped.items(),
            key=lambda item: item[0],
        ):
            if table in {
                "projects",
                "project_revisions",
                "workspace_sessions",
                "workspace_panels",
            }:
                continue
            if table in uuid_tables:
                await connection.execute(
                    text(
                        f"DELETE FROM {table} "
                        "WHERE tenant_id=:tenant AND id = ANY(:ids)"
                    ),
                    {
                        "tenant": tenant_id,
                        "ids": [UUID(value) for value in target_ids],
                    },
                )
            elif table in bigint_tables:
                await connection.execute(
                    text(
                        f"DELETE FROM {table} "
                        "WHERE tenant_id=:tenant AND id = ANY(:ids)"
                    ),
                    {
                        "tenant": tenant_id,
                        "ids": [int(value) for value in target_ids],
                    },
                )
            elif table in text_id_tables:
                tenant_clause = (
                    "tenant_id=:tenant AND "
                    if table in tenant_text_id_tables
                    else ""
                )
                await connection.execute(
                    text(
                        f"DELETE FROM {table} WHERE "
                        f"{tenant_clause}id = ANY(:ids)"
                    ),
                    {"tenant": tenant_id, "ids": target_ids},
                )
            elif table == "auth_sessions":
                await connection.execute(
                    text(
                        "DELETE FROM auth_sessions "
                        "WHERE tenant_id=:tenant AND token_id = ANY(:ids)"
                    ),
                    {"tenant": tenant_id, "ids": target_ids},
                )
            elif table == "auth_invite_codes":
                await connection.execute(
                    text(
                        "DELETE FROM auth_invite_codes "
                        "WHERE tenant_id=:tenant AND code = ANY(:ids)"
                    ),
                    {"tenant": tenant_id, "ids": target_ids},
                )
            elif table == "connector_oauth_states":
                await connection.execute(
                    text(
                        "DELETE FROM connector_oauth_states "
                        "WHERE tenant_id=:tenant AND state = ANY(:ids)"
                    ),
                    {"tenant": tenant_id, "ids": target_ids},
                )
            elif table == "connector_tokens":
                await connection.execute(
                    text(
                        "DELETE FROM connector_tokens "
                        "WHERE tenant_id=:tenant AND connector='fusion360'"
                    ),
                    {"tenant": tenant_id},
                )
            elif table == "knowledge_nodes":
                await connection.execute(
                    text(
                        "DELETE FROM knowledge_nodes "
                        "WHERE tenant_id=:tenant "
                        "AND customer_id || ':' || id = ANY(:ids)"
                    ),
                    {"tenant": tenant_id, "ids": target_ids},
                )
            else:
                raise LegacyRollbackUnsafe(
                    f"rollback has no deletion rule for {table}"
                )
        if project_ids:
            try:
                # Only untouched trigger-created projections may be removed.
                # Child foreign keys deliberately prevent deleting collaboration,
                # annotations, engineering/release or Bridge evidence.
                await connection.execute(
                    text('DELETE FROM cloud_documents WHERE project_id = ANY(:ids) AND state_version=0 AND event_sequence=0'),
                    {'ids':list(project_ids)},
                )
            except IntegrityError as exc:
                raise LegacyRollbackUnsafe('imported documents contain post-import collaboration or engineering data') from exc
            if await connection.scalar(text('SELECT count(*) FROM cloud_documents WHERE project_id = ANY(:ids)'),{'ids':list(project_ids)}):
                raise LegacyRollbackUnsafe('imported documents contain post-import state changes')
            await connection.execute(
                text(
                    "DELETE FROM legacy_snapshot_mappings "
                    "WHERE project_id = ANY(:ids)"
                ),
                {"ids": list(project_ids)},
            )
            await connection.execute(
                text(
                    "DELETE FROM projects "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": list(project_ids)},
            )
        await connection.execute(
            text(
                """
                DELETE FROM legacy_quarantine_records
                WHERE source_fingerprint=:fingerprint
                """
            ),
            {"fingerprint": source_fingerprint},
        )
        await connection.execute(
            text(
                """
                DELETE FROM legacy_import_mappings
                WHERE import_run_id IN (
                    SELECT id FROM legacy_import_runs
                    WHERE source_fingerprint=:fingerprint
                )
                """
            ),
            {"fingerprint": source_fingerprint},
        )
        await connection.execute(
            text(
                """
                UPDATE legacy_import_runs
                SET status='rolled_back', completed_at=CURRENT_TIMESTAMP
                WHERE source_fingerprint=:fingerprint
                """
            ),
            {"fingerprint": source_fingerprint},
        )

    deleted_objects = 0
    for key in sorted(set(object_keys)):
        await delete_object(key)
        deleted_objects += 1
    return {
        "source_fingerprint": source_fingerprint,
        "status": "rolled_back",
        "deleted_objects": deleted_objects,
    }
