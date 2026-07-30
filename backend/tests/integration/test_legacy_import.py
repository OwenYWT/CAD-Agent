"""Real PostgreSQL + MinIO tests for the one-time legacy migration."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import text

from app.config import settings
from app.db import close_database, get_database_engine
from app.db import auth_transaction
from app.migrations.legacy_import import (
    LegacyImportCollision,
    LegacySourcePaths,
    import_legacy_data,
    rollback_legacy_import,
)
from app.object_store import head_object


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not settings.object_store_endpoint_url,
    reason="real PostgreSQL and S3-compatible storage are required",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    if not TEST_DATABASE_URL:
        yield
        return
    original = settings.database_url
    settings.database_url = TEST_DATABASE_URL
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield
    settings.database_url = original


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def database_engine_lifecycle(migrated_database):
    yield
    await close_database()


def _create_sources(root: Path) -> tuple[LegacySourcePaths, dict[str, str]]:
    suffix = uuid4().hex
    now = datetime.now(timezone.utc)
    history_db = root / "history.db"
    auth_db = root / "auth.db"
    files = root / "files"
    session_id = f"session-{suffix}"
    panel_id = f"panel-{suffix}"
    orphan_panel_id = f"orphan-panel-{suffix}"
    request_id = f"request-{suffix}"

    auth = sqlite3.connect(auth_db)
    auth.executescript(
        """
        CREATE TABLE auth_sessions (
            token_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE invite_codes (
            code TEXT PRIMARY KEY,
            max_uses INTEGER NOT NULL,
            used_count INTEGER NOT NULL,
            expires_at TEXT,
            disabled_at TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    auth.execute(
        "INSERT INTO auth_sessions VALUES (?, ?, ?, NULL, ?)",
        (
            f"token-{suffix}",
            f"missing-user-{suffix}",
            (now + timedelta(hours=1)).isoformat(),
            now.isoformat(),
        ),
    )
    invite_code = f"IMPORT-{suffix[:12]}".upper()
    auth.execute(
        "INSERT INTO invite_codes VALUES (?, 3, 1, NULL, NULL, ?)",
        (invite_code, now.isoformat()),
    )
    auth.commit()
    auth.close()

    history = sqlite3.connect(history_db)
    history.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, user_id TEXT, title TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE panels (
            id TEXT PRIMARY KEY, session_id TEXT, title TEXT,
            current_code TEXT, current_params TEXT, created_at TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, panel_id TEXT, role TEXT,
            content TEXT, result TEXT, created_at TEXT
        );
        CREATE TABLE model_snapshots (
            id TEXT PRIMARY KEY, panel_id TEXT, parent_snapshot_id TEXT,
            version INTEGER, source TEXT, prompt TEXT, code TEXT,
            result TEXT, files TEXT, params TEXT, parameters TEXT,
            validation TEXT, inspect_report TEXT, repair_history TEXT,
            status TEXT, created_at TEXT
        );
        """
    )
    history.execute(
        "INSERT INTO sessions VALUES (?, NULL, ?, ?, ?)",
        (session_id, "真实迁移测试", now.isoformat(), now.isoformat()),
    )
    history.execute(
        "INSERT INTO panels VALUES (?, ?, ?, ?, NULL, ?)",
        (panel_id, session_id, "机械设计", "result = box()", now.isoformat()),
    )
    history.execute(
        "INSERT INTO messages VALUES (1, ?, 'user', ?, NULL, ?)",
        (panel_id, "创建一个测试零件", now.isoformat()),
    )
    snapshot_columns = (
        "id, panel_id, parent_snapshot_id, version, source, prompt, code, "
        "result, files, params, parameters, validation, inspect_report, "
        "repair_history, status, created_at"
    )
    for index, (target_panel, target_request) in enumerate(
        (
            (panel_id, request_id),
            (orphan_panel_id, f"orphan-request-{suffix}"),
        ),
        start=1,
    ):
        history.execute(
            f"INSERT INTO model_snapshots ({snapshot_columns}) "
            "VALUES (?, ?, NULL, 1, 'execute_code', ?, ?, ?, ?, NULL, "
            "NULL, ?, NULL, '[]', 'pass', ?)",
            (
                f"snapshot-{index}-{suffix}",
                target_panel,
                "创建测试模型",
                "result = box()",
                json.dumps(
                    {"request_id": target_request, "success": True},
                    separators=(",", ":"),
                ),
                json.dumps(
                    {"step": f"/api/files/{target_request}/result.step"},
                    separators=(",", ":"),
                ),
                json.dumps({"printable": True}, separators=(",", ":")),
                now.isoformat(),
            ),
        )
    history.commit()
    history.close()

    request_dir = files / request_id
    request_dir.mkdir(parents=True)
    (request_dir / "result.step").write_bytes(
        b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
    )
    paths = LegacySourcePaths(
        auth_db=auth_db,
        history_db=history_db,
        generated_files_root=files,
    )
    return paths, {
        "session_id": session_id,
        "panel_id": panel_id,
        "orphan_panel_id": orphan_panel_id,
        "invite_code": invite_code,
    }


@pytest.mark.asyncio(loop_scope="module")
async def test_import_replay_collision_quarantine_and_rollback(tmp_path):
    paths, ids = _create_sources(tmp_path)
    first = await import_legacy_data(paths)
    second = await import_legacy_data(paths)
    assert first.as_dict() == second.as_dict()
    assert first.target_counts["project_revisions"] == 2
    assert first.target_counts["projects"] == 2
    assert first.target_counts["auth_sessions"] == 1
    assert first.target_counts["auth_users"] == 1
    assert first.target_counts["project_files"] == 1
    assert first.target_counts["legacy_quarantine_records"] >= 3

    async with get_database_engine().connect() as connection:
        mapping_count = await connection.scalar(
            text(
                """
                SELECT count(*) FROM legacy_import_mappings
                WHERE import_run_id IN (
                    SELECT id FROM legacy_import_runs
                    WHERE source_fingerprint=:fingerprint
                )
                """
            ),
            {"fingerprint": first.source_fingerprint},
        )
        assert int(mapping_count or 0) > 0
        assert (
            await connection.scalar(
                text(
                    """
                    SELECT count(*) FROM legacy_snapshot_mappings
                    WHERE panel_id IN (:panel, :orphan)
                    """
                ),
                {
                    "panel": ids["panel_id"],
                    "orphan": ids["orphan_panel_id"],
                },
            )
            == 2
        )
        file_row = (
            await connection.execute(
                text(
                    """
                    SELECT object_key, sha256, size_bytes
                    FROM project_files
                    WHERE request_id LIKE 'request-%'
                      AND object_key LIKE 'legacy/%'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
            )
        ).mappings().one()
        object_key = file_row["object_key"]
    stored = await head_object(object_key)
    assert stored["size_bytes"] == file_row["size_bytes"]
    assert stored["metadata"]["sha256"] == file_row["sha256"]

    history = sqlite3.connect(paths.history_db)
    history.execute(
        "UPDATE panels SET title='changed-after-import' WHERE id=?",
        (ids["panel_id"],),
    )
    history.commit()
    history.close()
    with pytest.raises(LegacyImportCollision):
        await import_legacy_data(paths)

    rolled = await rollback_legacy_import(first.source_fingerprint)
    assert rolled["status"] == "rolled_back"
    assert rolled["deleted_objects"] == 1
    async with get_database_engine().connect() as connection:
        assert (
            await connection.scalar(
                text(
                    """
                    SELECT count(*) FROM legacy_import_mappings
                    WHERE import_run_id IN (
                        SELECT id FROM legacy_import_runs
                        WHERE source_fingerprint=:fingerprint
                    )
                    """
                ),
                {"fingerprint": first.source_fingerprint},
            )
            == 0
        )
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM workspace_sessions WHERE id=:id"
                ),
                {"id": ids["session_id"]},
            )
            == 0
        )
    with pytest.raises(ClientError):
        await head_object(object_key)


@pytest.mark.asyncio(loop_scope="module")
async def test_import_reports_existing_global_invite_collision(tmp_path):
    paths, ids = _create_sources(tmp_path)
    conflicting_tenant = uuid4()
    async with auth_transaction() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO auth_invite_codes (
                    tenant_id, code, max_uses, used_count
                ) VALUES (:tenant, :code, 1, 0)
                """
            ),
            {
                "tenant": conflicting_tenant,
                "code": ids["invite_code"],
            },
        )
    with pytest.raises(
        LegacyImportCollision,
        match="collides with existing authentication data",
    ):
        await import_legacy_data(paths)
