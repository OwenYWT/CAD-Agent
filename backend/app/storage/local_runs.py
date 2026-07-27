"""SQLite-backed M0 workflow state.

Execution remains process-local in M0.  This module persists only the workflow
identity, lifecycle, progress events, cancellation intent, and terminal result
so a client can reconnect and a restarted API can report interrupted work
truthfully.  It deliberately does not attempt to resume lost Python coroutines.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pydantic import BaseModel

from app.config import settings
from app.storage import history


ACTIVE_STATES = {
    "PENDING",
    "RUNNING",
    "INTERRUPTED",
    "RECONCILING",
    "CANCEL_REQUESTED",
}
TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED"}

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(value: str | None, default=None):
    if value is None:
        return default
    return json.loads(value)


def owner_identity(owner: str | None) -> str:
    """Return a stable comparison key without persisting API keys or JWTs."""
    if not owner:
        return "anonymous"
    digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


async def initialize() -> None:
    # aiosqlite serializes statements on its worker thread; CREATE IF NOT EXISTS
    # is safe when request and workflow tasks initialize concurrently.  Avoid an
    # asyncio.Lock here because TestClient and CLI probes legitimately use
    # different event loops against the same connection.
    db = await history.get_db()
    await db.executescript(
        """
            CREATE TABLE IF NOT EXISTS local_workflow_runs (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                scope_key TEXT,
                state TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT,
                terminal_result_ref TEXT,
                terminal_result_committed INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                updated_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS local_workflow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_run_id TEXT NOT NULL
                    REFERENCES local_workflow_runs(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                state TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_local_runs_owner_updated
                ON local_workflow_runs(owner_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_local_runs_scope_updated
                ON local_workflow_runs(owner_id, scope_key, updated_at);
            CREATE INDEX IF NOT EXISTS idx_local_runs_state
                ON local_workflow_runs(state, updated_at);
            CREATE INDEX IF NOT EXISTS idx_local_events_run_id
                ON local_workflow_events(workflow_run_id, id);
        """
    )
    await db.commit()


async def _append_event(
    run_id: str,
    event_type: str,
    *,
    state: str | None = None,
    payload: Any = None,
) -> None:
    db = await history.get_db()
    await db.execute(
        """
        INSERT INTO local_workflow_events
            (workflow_run_id, event_type, state, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, event_type, state, _json_dump(payload), _now()),
    )
    await db.commit()


async def create_run(
    *,
    kind: str,
    owner: str | None,
    request: dict,
    scope_key: str | None = None,
    run_id: str | None = None,
) -> str:
    await initialize()
    run_id = run_id or str(uuid.uuid4())
    now = _now()
    db = await history.get_db()
    await db.execute(
        """
        INSERT INTO local_workflow_runs (
            id, owner_id, kind, scope_key, state, request_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)
        """,
        (
            run_id,
            owner_identity(owner),
            kind,
            scope_key,
            _json_dump(request),
            now,
            now,
        ),
    )
    await db.commit()
    await _append_event(run_id, "state", state="PENDING")
    return run_id


async def _transition(
    run_id: str,
    state: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    now = _now()
    started_at = now if state == "RUNNING" else None
    finished_at = now if state in TERMINAL_STATES else None
    db = await history.get_db()
    await db.execute(
        """
        UPDATE local_workflow_runs
        SET state = ?,
            started_at = COALESCE(started_at, ?),
            error_code = ?,
            error_message = ?,
            updated_at = ?,
            finished_at = COALESCE(?, finished_at)
        WHERE id = ?
        """,
        (
            state,
            started_at,
            error_code,
            error_message,
            now,
            finished_at,
            run_id,
        ),
    )
    await db.commit()
    await _append_event(run_id, "state", state=state)


async def mark_running(run_id: str) -> None:
    await initialize()
    await _transition(run_id, "RUNNING")


async def append_progress(run_id: str, payload: Any) -> None:
    await initialize()
    await _append_event(run_id, "progress", payload=payload)


async def commit_result(
    run_id: str,
    result: BaseModel | dict,
    *,
    state: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict:
    await initialize()
    if isinstance(result, BaseModel):
        result_data = result.model_dump(mode="json")
    else:
        result_data = dict(result)
    # A runner returning a structured business result completed its workflow,
    # even when the CAD result says success=false (matching the legacy async API).
    # Thrown exceptions, restart reconciliation, and cancellation pass an
    # explicit FAILED/CANCELLED state.
    terminal_state = state or "COMPLETED"
    if terminal_state not in TERMINAL_STATES:
        raise ValueError(f"terminal state required, got {terminal_state}")
    error = result_data.get("error") or {}
    code = error_code or error.get("type")
    message = error_message or error.get("message")
    now = _now()
    db = await history.get_db()
    await db.execute(
        """
        UPDATE local_workflow_runs
        SET state = ?,
            result_json = ?,
            terminal_result_ref = ?,
            terminal_result_committed = 1,
            error_code = ?,
            error_message = ?,
            updated_at = ?,
            finished_at = ?
        WHERE id = ?
        """,
        (
            terminal_state,
            _json_dump(result_data),
            result_data.get("request_id"),
            code,
            message,
            now,
            now,
            run_id,
        ),
    )
    await db.commit()
    await _append_event(run_id, "state", state=terminal_state)
    return result_data


async def request_cancel(run_id: str, owner: str | None) -> bool:
    await initialize()
    db = await history.get_db()
    cursor = await db.execute(
        """
        UPDATE local_workflow_runs
        SET cancel_requested = 1, state = 'CANCEL_REQUESTED', updated_at = ?
        WHERE id = ? AND owner_id = ? AND state IN ('PENDING', 'RUNNING')
        """,
        (_now(), run_id, owner_identity(owner)),
    )
    await db.commit()
    if cursor.rowcount != 1:
        return False
    await _append_event(run_id, "state", state="CANCEL_REQUESTED")
    return True


def _run_from_row(row) -> dict:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "kind": row["kind"],
        "scope_key": row["scope_key"],
        "state": row["state"],
        "request": _json_load(row["request_json"], {}),
        "result": _json_load(row["result_json"]),
        "terminal_result_ref": row["terminal_result_ref"],
        "terminal_result_committed": bool(row["terminal_result_committed"]),
        "cancel_requested": bool(row["cancel_requested"]),
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
    }


async def get_run(run_id: str, owner: str | None) -> dict | None:
    await initialize()
    db = await history.get_db()
    cursor = await db.execute(
        "SELECT * FROM local_workflow_runs WHERE id = ? AND owner_id = ?",
        (run_id, owner_identity(owner)),
    )
    row = await cursor.fetchone()
    return _run_from_row(row) if row else None


async def get_run_internal(run_id: str) -> dict | None:
    await initialize()
    db = await history.get_db()
    cursor = await db.execute(
        "SELECT * FROM local_workflow_runs WHERE id = ?",
        (run_id,),
    )
    row = await cursor.fetchone()
    return _run_from_row(row) if row else None


async def find_latest_for_scope(
    scope_key: str,
    owner: str | None,
    *,
    active_only: bool = False,
) -> dict | None:
    await initialize()
    conditions = ""
    params: list[Any] = [owner_identity(owner), scope_key]
    if active_only:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        conditions = f" AND state IN ({placeholders})"
        params.extend(sorted(ACTIVE_STATES))
    db = await history.get_db()
    cursor = await db.execute(
        f"""
        SELECT * FROM local_workflow_runs
        WHERE owner_id = ? AND scope_key = ? {conditions}
        ORDER BY created_at DESC
        LIMIT 1
        """,
        params,
    )
    row = await cursor.fetchone()
    return _run_from_row(row) if row else None


async def list_events(run_id: str) -> list[dict]:
    await initialize()
    db = await history.get_db()
    rows = await db.execute_fetchall(
        """
        SELECT id, workflow_run_id, event_type, state, payload_json, created_at
        FROM local_workflow_events
        WHERE workflow_run_id = ?
        ORDER BY id
        """,
        (run_id,),
    )
    return [
        {
            "id": row["id"],
            "workflow_run_id": row["workflow_run_id"],
            "event_type": row["event_type"],
            "state": row["state"],
            "payload": _json_load(row["payload_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _committed_result_is_verifiable(run: dict) -> bool:
    result = run.get("result")
    if not isinstance(result, dict):
        return False
    request_id = result.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return False
    if result.get("success") is not True:
        return isinstance(result.get("error"), dict)
    files = result.get("files")
    if not isinstance(files, dict) or not files:
        return False
    storage_root = Path(settings.file_storage_dir).expanduser().resolve()
    for url in files.values():
        if not isinstance(url, str):
            return False
        path = unquote(urlparse(url).path)
        prefix = f"/api/files/{request_id}/"
        if not path.startswith(prefix):
            return False
        filename = path[len(prefix):]
        if not filename or Path(filename).name != filename:
            return False
        artifact = (storage_root / request_id / filename).resolve()
        if artifact.parent != (storage_root / request_id).resolve():
            return False
        if not artifact.is_file() or artifact.stat().st_size <= 0:
            return False
    return True


async def reconcile_incomplete_runs() -> list[str]:
    """Fail uncommitted process-local work; never pretend that it resumed."""
    await initialize()
    db = await history.get_db()
    rows = await db.execute_fetchall(
        """
        SELECT * FROM local_workflow_runs
        WHERE state IN ('PENDING', 'RUNNING', 'INTERRUPTED', 'RECONCILING',
                        'CANCEL_REQUESTED')
        ORDER BY created_at
        """
    )
    reconciled: list[str] = []
    for row in rows:
        run = _run_from_row(row)
        run_id = run["id"]
        reconciled.append(run_id)
        if run["terminal_result_committed"] and _committed_result_is_verifiable(run):
            terminal = "COMPLETED" if run["result"]["success"] else "FAILED"
            await _transition(
                run_id,
                terminal,
                error_code=run["error_code"],
                error_message=run["error_message"],
            )
            continue

        if run["state"] != "INTERRUPTED":
            await _transition(run_id, "INTERRUPTED")
        await _transition(run_id, "RECONCILING")
        failure = {
            "request_id": run_id,
            "success": False,
            "files": {},
            "execution_time_ms": 0,
            "attempts": 0,
            "error": {
                "type": "ProcessRestarted",
                "message": "服务进程已重启，未完成的本地计算无法恢复，请重新提交。",
            },
        }
        await commit_result(
            run_id,
            failure,
            state="FAILED",
            error_code="process_restarted",
            error_message=failure["error"]["message"],
        )
    return reconciled
