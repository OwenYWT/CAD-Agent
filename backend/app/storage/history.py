import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.agent.assembly_manifest import changed_part_ids, enrich_assembly_parts
from app.config import settings

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is not None:
        return _db
    async with _db_lock:
        if _db is not None:
            return _db
        db_path = Path(settings.history_db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(str(db_path))
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        # Enable FK enforcement so the ON DELETE CASCADE declared on panels/messages
        # actually fires — otherwise delete_session() orphans panels+messages on disk
        # (privacy leak + unbounded growth). SQLite needs this per-connection.
        await _db.execute("PRAGMA foreign_keys=ON")
        await _init_tables(_db)
    return _db


async def close_db():
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def _init_tables(db: aiosqlite.Connection):
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            title TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS panels (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT '',
            current_code TEXT,
            current_params TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            panel_id TEXT NOT NULL REFERENCES panels(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            result TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS model_snapshots (
            id TEXT PRIMARY KEY,
            panel_id TEXT NOT NULL REFERENCES panels(id) ON DELETE CASCADE,
            parent_snapshot_id TEXT REFERENCES model_snapshots(id) ON DELETE SET NULL,
            version INTEGER NOT NULL,
            source TEXT NOT NULL,
            prompt TEXT NOT NULL DEFAULT '',
            code TEXT NOT NULL,
            result TEXT NOT NULL,
            files TEXT NOT NULL DEFAULT '{}',
            params TEXT,
            parameters TEXT,
            validation TEXT,
            inspect_report TEXT,
            repair_history TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            rating TEXT,            -- "up" | "down" | NULL
            printed TEXT,           -- "yes" | "no" | "not_yet" | NULL  (ground truth the system cannot infer)
            note TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS onshape_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            user_id TEXT,
            document_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            element_id TEXT,
            translation_id TEXT,
            status TEXT NOT NULL,
            onshape_url TEXT NOT NULL,
            document_name TEXT,
            step_filename TEXT,
            mode TEXT NOT NULL DEFAULT 'import_step',
            raw_response TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            panel_id TEXT,
            user_prompt TEXT NOT NULL,
            capability TEXT NOT NULL DEFAULT 'auto',
            status TEXT NOT NULL,
            current_step_id TEXT,
            result_request_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_steps (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            step_index INTEGER NOT NULL,
            step_type TEXT NOT NULL,
            status TEXT NOT NULL,
            input_json TEXT NOT NULL,
            output_json TEXT,
            error_json TEXT,
            started_at TEXT,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_artifacts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            step_id TEXT NOT NULL REFERENCES agent_steps(id) ON DELETE CASCADE,
            artifact_type TEXT NOT NULL,
            path TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_panels_session ON panels(session_id);
        CREATE INDEX IF NOT EXISTS idx_messages_panel ON messages(panel_id);
        CREATE INDEX IF NOT EXISTS idx_model_snapshots_panel_version ON model_snapshots(panel_id, version);
        CREATE INDEX IF NOT EXISTS idx_model_snapshots_panel_created ON model_snapshots(panel_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_feedback_request ON feedback(request_id);
        CREATE INDEX IF NOT EXISTS idx_onshape_links_request ON onshape_links(request_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_onshape_links_user ON onshape_links(user_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_session_panel ON agent_runs(session_id, panel_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_agent_steps_run_index ON agent_steps(run_id, step_index);
        CREATE INDEX IF NOT EXISTS idx_agent_steps_status ON agent_steps(status, started_at);
        CREATE INDEX IF NOT EXISTS idx_agent_artifacts_run ON agent_artifacts(run_id, created_at);
    """)
    columns = await db.execute_fetchall("PRAGMA table_info(sessions)")
    if "user_id" not in {row["name"] for row in columns}:
        await db.execute("ALTER TABLE sessions ADD COLUMN user_id TEXT")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at)")
    await db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value)


def _json_load(value, default=None):
    if value is None:
        return default
    return json.loads(value)


def _snapshot_status(result: dict) -> str:
    if not result.get("success"):
        return "fail"
    verdict = (result.get("inspect_report") or {}).get("verdict")
    return verdict if verdict in {"pass", "warn", "fail"} else "unknown"


def _snapshot_from_row(row) -> dict:
    result = _json_load(row["result"], {})
    inspect_report = _json_load(row["inspect_report"], None)
    assembly_parts = result.get("assembly_parts") or []
    return {
        "id": row["id"],
        "panel_id": row["panel_id"],
        "parent_snapshot_id": row["parent_snapshot_id"],
        "version": row["version"],
        "source": row["source"],
        "prompt": row["prompt"],
        "code": row["code"],
        "result": result,
        "files": _json_load(row["files"], {}),
        "params": _json_load(row["params"], None),
        "parameters": _json_load(row["parameters"], None),
        "validation": _json_load(row["validation"], None),
        "inspect_report": inspect_report,
        "repair_history": _json_load(row["repair_history"], []),
        "status": row["status"],
        "created_at": row["created_at"],
        "available_exports": (inspect_report or {}).get("available_exports", []),
        "inspect_verdict": (inspect_report or {}).get("verdict"),
        "assembly_parts": enrich_assembly_parts(assembly_parts) if assembly_parts else [],
    }


# ---- Sessions ----

async def create_session(session_id: str, title: str = "", user_id: str | None = None) -> dict:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.create_session(session_id, title, user_id)
    db = await get_db()
    now = _now()
    await db.execute(
        "INSERT OR IGNORE INTO sessions (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, user_id, title, now, now),
    )
    if user_id:
        await db.execute(
            "UPDATE sessions SET user_id = COALESCE(user_id, ?) WHERE id = ?",
            (user_id, session_id),
        )
    if title.strip():
        await db.execute(
            """
            UPDATE sessions
            SET title = ?, updated_at = ?
            WHERE id = ? AND (title IS NULL OR TRIM(title) = '')
            """,
            (title, now, session_id),
        )
    await db.commit()
    cursor = await db.execute(
        "SELECT id, user_id, title, created_at, updated_at FROM sessions WHERE id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    return dict(row)


async def list_sessions(user_id: str | None = None) -> list[dict]:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.list_sessions(user_id)
    db = await get_db()
    if user_id:
        rows = await db.execute_fetchall(
            "SELECT id, title, created_at, updated_at FROM sessions WHERE user_id = ? ORDER BY updated_at DESC LIMIT 50",
            (user_id,),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT 50"
        )
    return [dict(r) for r in rows]


async def session_belongs_to_user(session_id: str, user_id: str | None) -> bool:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.session_belongs_to_user(
            session_id,
            user_id,
        )
    if not user_id:
        return True
    db = await get_db()
    cursor = await db.execute("SELECT user_id FROM sessions WHERE id = ?", (session_id,))
    row = await cursor.fetchone()
    return bool(row and row["user_id"] == user_id)


async def session_writable_by_user(session_id: str, user_id: str | None) -> bool:
    """Write-side ownership check (used by the WebSocket).

    Differs from session_belongs_to_user: a session that does NOT exist yet is
    writable (the caller is about to create it). A session is only refused when it
    already exists AND is owned by a *different* user. Sessions with a NULL owner
    (created in dev/anonymous mode) are claimable by the first authenticated writer.
    """
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.session_writable_by_user(
            session_id,
            user_id,
        )
    if not user_id:
        return True
    db = await get_db()
    cursor = await db.execute("SELECT user_id FROM sessions WHERE id = ?", (session_id,))
    row = await cursor.fetchone()
    if row is None:
        return True  # new session_id — caller creates it
    return row["user_id"] is None or row["user_id"] == user_id


async def panel_writable_by_session(
    panel_id: str,
    session_id: str,
    user_id: str | None,
) -> bool:
    """Allow a new panel ID, or the existing panel only in its original session.

    Panel IDs are global primary keys. Reusing one in another session would make
    INSERT OR IGNORE route later messages into the old session.
    """
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.panel_writable_by_session(
            panel_id,
            session_id,
            user_id,
        )
    db = await get_db()
    cursor = await db.execute(
        """
        SELECT p.session_id, s.user_id
        FROM panels p
        JOIN sessions s ON s.id = p.session_id
        WHERE p.id = ?
        """,
        (panel_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return True
    if row["session_id"] != session_id:
        return False
    return not user_id or row["user_id"] is None or row["user_id"] == user_id


async def panel_belongs_to_user(panel_id: str, user_id: str | None) -> bool:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.panel_belongs_to_user(
            panel_id,
            user_id,
        )
    if not user_id:
        return True
    db = await get_db()
    cursor = await db.execute(
        "SELECT s.user_id FROM panels p JOIN sessions s ON p.session_id = s.id WHERE p.id = ?",
        (panel_id,),
    )
    row = await cursor.fetchone()
    return bool(row and row["user_id"] == user_id)


async def delete_session(session_id: str, user_id: str | None = None):
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.delete_session(session_id, user_id)
    db = await get_db()
    if user_id:
        await db.execute("DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id))
    else:
        await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    await db.commit()


async def touch_session(session_id: str):
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.touch_session(session_id)
    db = await get_db()
    await db.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
    )
    await db.commit()


# ---- Panels ----

async def create_panel(session_id: str, panel_id: str, title: str = "", user_id: str | None = None) -> dict:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.create_panel(
            session_id,
            panel_id,
            title,
            user_id,
        )
    db = await get_db()
    existing = await db.execute(
        "SELECT id, session_id, title, created_at FROM panels WHERE id = ?",
        (panel_id,),
    )
    existing_row = await existing.fetchone()
    if existing_row is not None and existing_row["session_id"] != session_id:
        raise ValueError("panel_id already belongs to another session")

    now = _now()
    await create_session(session_id, user_id=user_id)
    await db.execute(
        "INSERT OR IGNORE INTO panels (id, session_id, title, created_at) VALUES (?, ?, ?, ?)",
        (panel_id, session_id, title, now),
    )
    await db.commit()
    cursor = await db.execute(
        "SELECT id, session_id, title, created_at FROM panels WHERE id = ?",
        (panel_id,),
    )
    row = await cursor.fetchone()
    if row is None or row["session_id"] != session_id:
        raise ValueError("panel_id already belongs to another session")
    return dict(row)


async def list_panels(session_id: str) -> list[dict]:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.list_panels(session_id)
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, session_id, title, current_code, created_at FROM panels WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    )
    return [dict(r) for r in rows]


async def update_panel_code(panel_id: str, code: str | None, params: dict | None = None):
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.update_panel_code(panel_id, code, params)
    db = await get_db()
    await db.execute(
        "UPDATE panels SET current_code = ?, current_params = ? WHERE id = ?",
        (code, json.dumps(params) if params else None, panel_id),
    )
    await db.commit()


# ---- Messages ----

async def save_message(
    panel_id: str, role: str, content: str, result: dict | None = None
):
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.save_message(
            panel_id,
            role,
            content,
            result,
        )
    db = await get_db()
    await db.execute(
        "INSERT INTO messages (panel_id, role, content, result, created_at) VALUES (?, ?, ?, ?, ?)",
        (panel_id, role, content, json.dumps(result) if result else None, _now()),
    )
    await db.commit()


async def get_messages(panel_id: str) -> list[dict]:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.get_messages(panel_id)
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT role, content, result FROM messages WHERE panel_id = ? ORDER BY id",
        (panel_id,),
    )
    result = []
    for r in rows:
        msg = {"role": r["role"], "content": r["content"]}
        if r["result"]:
            msg["result"] = json.loads(r["result"])
        result.append(msg)
    return result


# ---- Model snapshots ----

async def create_model_snapshot(
    panel_id: str,
    result: dict,
    source: str,
    prompt: str = "",
    parent_snapshot_id: str | None = None,
) -> dict:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.create_model_snapshot(
            panel_id,
            result,
            source,
            prompt,
            parent_snapshot_id,
        )
    db = await get_db()
    snapshot_id = str(uuid.uuid4())
    now = _now()
    status = _snapshot_status(result)
    code = result.get("code") or ""
    files = result.get("files") or {}
    params = result.get("params")
    parameters = result.get("parameters")
    validation = result.get("validation")
    inspect_report = result.get("inspect_report")
    repair_history = result.get("repair_history") or []
    await db.execute(
        """
        INSERT INTO model_snapshots (
            id, panel_id, parent_snapshot_id, version, source, prompt, code,
            result, files, params, parameters, validation, inspect_report,
            repair_history, status, created_at
        )
        SELECT ?, ?,
            COALESCE(
                ?,
                (
                    SELECT parent.id
                    FROM model_snapshots AS parent
                    WHERE parent.panel_id = ?
                    ORDER BY parent.version DESC
                    LIMIT 1
                )
            ),
            COALESCE(MAX(version), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        FROM model_snapshots
        WHERE panel_id = ?
        """,
        (
            snapshot_id,
            panel_id,
            parent_snapshot_id,
            panel_id,
            source,
            prompt,
            code,
            json.dumps(result),
            json.dumps(files),
            _json_dump(params),
            _json_dump(parameters),
            _json_dump(validation),
            _json_dump(inspect_report),
            json.dumps(repair_history),
            status,
            now,
            panel_id,
        ),
    )
    await db.commit()
    snapshot = await get_model_snapshot(snapshot_id)
    return snapshot


async def list_model_snapshots(panel_id: str) -> list[dict]:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.list_model_snapshots(panel_id)
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM model_snapshots WHERE panel_id = ? ORDER BY version DESC",
        (panel_id,),
    )
    return [_snapshot_from_row(row) for row in rows]


async def get_model_snapshot(
    snapshot_id: str,
    *,
    include_artifact_fingerprints: bool = False,
) -> dict | None:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.get_model_snapshot(
            snapshot_id,
            include_artifact_fingerprints=include_artifact_fingerprints,
        )
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM model_snapshots WHERE id = ?",
        (snapshot_id,),
    )
    row = await cursor.fetchone()
    return _snapshot_from_row(row) if row else None


async def snapshot_belongs_to_user(snapshot_id: str, user_id: str | None) -> bool:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.snapshot_belongs_to_user(
            snapshot_id,
            user_id,
        )
    if not user_id:
        return True
    db = await get_db()
    cursor = await db.execute(
        """
        SELECT s.user_id
        FROM model_snapshots ms
        JOIN panels p ON ms.panel_id = p.id
        JOIN sessions s ON p.session_id = s.id
        WHERE ms.id = ?
        """,
        (snapshot_id,),
    )
    row = await cursor.fetchone()
    return bool(row and row["user_id"] == user_id)


async def restore_model_snapshot(snapshot_id: str, user_id: str | None = None) -> dict | None:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.restore_model_snapshot(
            snapshot_id,
            user_id,
        )
    if not await snapshot_belongs_to_user(snapshot_id, user_id):
        return None
    snapshot = await get_model_snapshot(snapshot_id)
    if snapshot is None:
        return None
    restored_result = {
        **snapshot["result"],
        "snapshot_id": snapshot["id"],
        "version": snapshot["version"],
        "panel_id": snapshot["panel_id"],
    }
    db = await get_db()
    try:
        await db.execute(
            "UPDATE panels SET current_code = ?, current_params = ? WHERE id = ?",
            (
                snapshot["code"],
                _json_dump(snapshot.get("params")),
                snapshot["panel_id"],
            ),
        )
        await db.execute(
            "INSERT INTO messages (panel_id, role, content, result, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                snapshot["panel_id"],
                "assistant",
                f"已恢复模型版本 v{snapshot['version']}",
                json.dumps(restored_result),
                _now(),
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    snapshot["result"] = restored_result
    return snapshot


async def diff_model_snapshots(from_snapshot_id: str, to_snapshot_id: str, user_id: str | None = None) -> dict | None:
    if not await snapshot_belongs_to_user(from_snapshot_id, user_id):
        return None
    if not await snapshot_belongs_to_user(to_snapshot_id, user_id):
        return None
    before = await get_model_snapshot(from_snapshot_id, include_artifact_fingerprints=True)
    after = await get_model_snapshot(to_snapshot_id, include_artifact_fingerprints=True)
    if before is None or after is None:
        return None

    has_fingerprints = "_file_fingerprints" in before or "_file_fingerprints" in after
    before_files = before.get("_file_fingerprints") if has_fingerprints else before.get("files") or None
    after_files = after.get("_file_fingerprints") if has_fingerprints else after.get("files") or None

    def _parameters(snapshot: dict) -> dict | None:
        parameters = snapshot.get("parameters")
        if isinstance(parameters, list):
            values = {}
            for item in parameters:
                if not isinstance(item, dict) or not item.get("name") or "value" not in item:
                    return None
                name = item["name"]
                if name in values:
                    return None
                values[name] = {"value": item["value"], "unit": item.get("unit")}
            return values
        return snapshot.get("params")

    def _verdict(snapshot: dict) -> str | None:
        report = snapshot.get("inspect_report") or {}
        if report.get("verdict"):
            return report["verdict"]
        gates = (snapshot.get("validation") or {}).get("gates") or []
        active = [gate for gate in gates if gate.get("mode") in {"required", "advisory"}]
        if not active:
            return None
        if any(gate.get("mode") == "required" and gate.get("outcome") == "failed" for gate in active):
            return "fail"
        return "pass" if all(gate.get("outcome") == "passed" for gate in active) else "warn"

    before_params = _parameters(before)
    after_params = _parameters(after)
    before_parts = before.get("assembly_parts") or []
    after_parts = after.get("assembly_parts") or []
    part_changes = changed_part_ids(before_parts, after_parts)
    before_report = before.get("inspect_report") or {}
    after_report = after.get("inspect_report") or {}

    def _diff_keys(before_map: dict, after_map: dict) -> dict[str, list[str]]:
        before_keys = set(before_map)
        after_keys = set(after_map)
        changed = [
            key
            for key in sorted(before_keys & after_keys)
            if before_map.get(key) != after_map.get(key)
        ]
        unchanged = [
            key
            for key in sorted(before_keys & after_keys)
            if before_map.get(key) == after_map.get(key)
        ]
        return {
            "added": sorted(after_keys - before_keys),
            "removed": sorted(before_keys - after_keys),
            "changed": changed,
            "unchanged": unchanged,
        }

    result = {
        "from_snapshot_id": from_snapshot_id,
        "to_snapshot_id": to_snapshot_id,
        "model_changes": {
            "prompt_changed": before.get("prompt") != after.get("prompt"),
            "source_changed": before.get("source") != after.get("source"),
            "inspect_verdict": {
                "from": _verdict(before),
                "to": _verdict(after),
            },
            "bounding_box": {
                "from": before_report.get("bounding_box"),
                "to": after_report.get("bounding_box"),
            },
            "volume": {
                "from": before_report.get("volume"),
                "to": after_report.get("volume"),
            },
        },
    }
    if before.get("code") or after.get("code"):
        result["model_changes"]["code_changed"] = before.get("code") != after.get("code")
    if isinstance(before_files, dict) and isinstance(after_files, dict):
        result["file_changes"] = _diff_keys(before_files, after_files)
    if isinstance(before_params, dict) and isinstance(after_params, dict):
        result["parameter_changes"] = _diff_keys(before_params, after_params)
    if (
        isinstance(before.get("assembly_parts"), list)
        and isinstance(after.get("assembly_parts"), list)
        and (before_parts or after_parts)
    ):
        result["part_changes"] = part_changes
    return result


# ---- Feedback (tester ground-truth signal) ----

async def save_feedback(
    request_id: str,
    rating: str | None = None,
    printed: str | None = None,
    note: str | None = None,
) -> dict:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.save_feedback(
            request_id,
            rating,
            printed,
            note,
        )
    db = await get_db()
    now = _now()
    await db.execute(
        "INSERT INTO feedback (request_id, rating, printed, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (request_id, rating, printed, note, now),
    )
    await db.commit()
    return {
        "request_id": request_id,
        "rating": rating,
        "printed": printed,
        "note": note,
        "created_at": now,
    }


async def get_feedback(request_id: str) -> list[dict]:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.get_feedback(request_id)
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT request_id, rating, printed, note, created_at FROM feedback "
        "WHERE request_id = ? ORDER BY id",
        (request_id,),
    )
    return [dict(r) for r in rows]


# ---- Onshape links ----

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
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.save_onshape_link(
            request_id,
            user_id,
            document_id,
            workspace_id,
            element_id,
            translation_id,
            status,
            onshape_url,
            document_name,
            step_filename,
            mode,
            raw_response,
        )
    db = await get_db()
    now = _now()
    raw_text = json.dumps(raw_response, ensure_ascii=False) if raw_response is not None else None
    await db.execute(
        """
        INSERT INTO onshape_links (
            request_id, user_id, document_id, workspace_id, element_id,
            translation_id, status, onshape_url, document_name, step_filename,
            mode, raw_response, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            user_id,
            document_id,
            workspace_id,
            element_id,
            translation_id,
            status,
            onshape_url,
            document_name,
            step_filename,
            mode,
            raw_text,
            now,
            now,
        ),
    )
    await db.commit()
    return {
        "request_id": request_id,
        "user_id": user_id,
        "document_id": document_id,
        "workspace_id": workspace_id,
        "element_id": element_id,
        "translation_id": translation_id,
        "status": status,
        "onshape_url": onshape_url,
        "document_name": document_name,
        "step_filename": step_filename,
        "mode": mode,
        "created_at": now,
        "updated_at": now,
    }


async def get_onshape_links(request_id: str, user_id: str | None = None) -> list[dict]:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.get_onshape_links(request_id, user_id)
    db = await get_db()
    if user_id:
        rows = await db.execute_fetchall(
            """
            SELECT request_id, status, onshape_url, document_id, workspace_id,
                   element_id, translation_id, document_name, step_filename,
                   mode, created_at, updated_at
            FROM onshape_links
            WHERE request_id = ? AND user_id = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (request_id, user_id),
        )
    else:
        rows = await db.execute_fetchall(
            """
            SELECT request_id, status, onshape_url, document_id, workspace_id,
                   element_id, translation_id, document_name, step_filename,
                   mode, created_at, updated_at
            FROM onshape_links
            WHERE request_id = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (request_id,),
        )
    return [dict(r) for r in rows]


async def get_latest_onshape_link(request_id: str, user_id: str | None = None) -> dict | None:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.get_latest_onshape_link(
            request_id,
            user_id,
        )
    db = await get_db()
    if user_id:
        cursor = await db.execute(
            """
            SELECT id, request_id, status, onshape_url, document_id, workspace_id,
                   element_id, translation_id, document_name, step_filename,
                   mode, created_at, updated_at
            FROM onshape_links
            WHERE request_id = ? AND user_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (request_id, user_id),
        )
    else:
        cursor = await db.execute(
            """
            SELECT id, request_id, status, onshape_url, document_id, workspace_id,
                   element_id, translation_id, document_name, step_filename,
                   mode, created_at, updated_at
            FROM onshape_links
            WHERE request_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (request_id,),
        )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_onshape_link_by_translation(
    translation_id: str,
    user_id: str | None = None,
) -> dict | None:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.get_onshape_link_by_translation(
            translation_id,
            user_id,
        )
    db = await get_db()
    if user_id:
        cursor = await db.execute(
            """
            SELECT id, request_id, status, onshape_url, document_id, workspace_id,
                   element_id, translation_id, document_name, step_filename,
                   mode, created_at, updated_at
            FROM onshape_links
            WHERE translation_id = ? AND user_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (translation_id, user_id),
        )
    else:
        cursor = await db.execute(
            """
            SELECT id, request_id, status, onshape_url, document_id, workspace_id,
                   element_id, translation_id, document_name, step_filename,
                   mode, created_at, updated_at
            FROM onshape_links
            WHERE translation_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (translation_id,),
        )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def update_onshape_link_status(
    link_id: int,
    status: str,
    element_id: str | None,
    onshape_url: str,
    raw_response: dict | None = None,
) -> dict | None:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_history

        return await postgres_history.update_onshape_link_status(
            link_id,
            status,
            element_id,
            onshape_url,
            raw_response,
        )
    db = await get_db()
    now = _now()
    raw_text = json.dumps(raw_response, ensure_ascii=False) if raw_response is not None else None
    await db.execute(
        """
        UPDATE onshape_links
        SET status = ?, element_id = COALESCE(?, element_id), onshape_url = ?,
            raw_response = COALESCE(?, raw_response), updated_at = ?
        WHERE id = ?
        """,
        (status, element_id, onshape_url, raw_text, now, link_id),
    )
    await db.commit()
    cursor = await db.execute(
        """
        SELECT request_id, status, onshape_url, document_id, workspace_id,
               element_id, translation_id, document_name, step_filename,
               mode, created_at, updated_at
        FROM onshape_links
        WHERE id = ?
        """,
        (link_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None
