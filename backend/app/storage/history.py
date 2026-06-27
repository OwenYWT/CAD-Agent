import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

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
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            rating TEXT,            -- "up" | "down" | NULL
            printed TEXT,           -- "yes" | "no" | "not_yet" | NULL  (ground truth the system cannot infer)
            note TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_panels_session ON panels(session_id);
        CREATE INDEX IF NOT EXISTS idx_messages_panel ON messages(panel_id);
        CREATE INDEX IF NOT EXISTS idx_feedback_request ON feedback(request_id);
    """)
    columns = await db.execute_fetchall("PRAGMA table_info(sessions)")
    if "user_id" not in {row["name"] for row in columns}:
        await db.execute("ALTER TABLE sessions ADD COLUMN user_id TEXT")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at)")
    await db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- Sessions ----

async def create_session(session_id: str, title: str = "", user_id: str | None = None) -> dict:
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
    await db.commit()
    return {"id": session_id, "user_id": user_id, "title": title, "created_at": now, "updated_at": now}


async def list_sessions(user_id: str | None = None) -> list[dict]:
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
    if not user_id:
        return True
    db = await get_db()
    cursor = await db.execute("SELECT user_id FROM sessions WHERE id = ?", (session_id,))
    row = await cursor.fetchone()
    return bool(row and row["user_id"] == user_id)


async def panel_belongs_to_user(panel_id: str, user_id: str | None) -> bool:
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
    db = await get_db()
    if user_id:
        await db.execute("DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id))
    else:
        await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    await db.commit()


async def touch_session(session_id: str):
    db = await get_db()
    await db.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
    )
    await db.commit()


# ---- Panels ----

async def create_panel(session_id: str, panel_id: str, title: str = "", user_id: str | None = None) -> dict:
    db = await get_db()
    now = _now()
    # Ensure session exists
    await create_session(session_id, user_id=user_id)
    await db.execute(
        "INSERT OR IGNORE INTO panels (id, session_id, title, created_at) VALUES (?, ?, ?, ?)",
        (panel_id, session_id, title, now),
    )
    await db.commit()
    return {"id": panel_id, "session_id": session_id, "title": title, "created_at": now}


async def list_panels(session_id: str) -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, session_id, title, current_code, created_at FROM panels WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    )
    return [dict(r) for r in rows]


async def update_panel_code(panel_id: str, code: str | None, params: dict | None = None):
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
    db = await get_db()
    await db.execute(
        "INSERT INTO messages (panel_id, role, content, result, created_at) VALUES (?, ?, ?, ?, ?)",
        (panel_id, role, content, json.dumps(result) if result else None, _now()),
    )
    await db.commit()


async def get_messages(panel_id: str) -> list[dict]:
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


# ---- Feedback (tester ground-truth signal) ----

async def save_feedback(
    request_id: str,
    rating: str | None = None,
    printed: str | None = None,
    note: str | None = None,
) -> dict:
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
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT request_id, rating, printed, note, created_at FROM feedback "
        "WHERE request_id = ? ORDER BY id",
        (request_id,),
    )
    return [dict(r) for r in rows]
