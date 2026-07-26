from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from app.storage import history

TERMINAL_RUN_STATUSES = {"succeeded", "failed", "blocked", "cancelled"}
TERMINAL_STEP_STATUSES = {"succeeded", "failed", "blocked", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False)


def _json_load(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def _run_from_row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "panel_id": row["panel_id"],
        "user_prompt": row["user_prompt"],
        "capability": row["capability"],
        "status": row["status"],
        "current_step_id": row["current_step_id"],
        "result_request_id": row["result_request_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


def _step_from_row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "step_index": row["step_index"],
        "step_type": row["step_type"],
        "status": row["status"],
        "input": _json_load(row["input_json"], {}),
        "output": _json_load(row["output_json"], None),
        "error": _json_load(row["error_json"], None),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


def _artifact_from_row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "step_id": row["step_id"],
        "artifact_type": row["artifact_type"],
        "path": row["path"],
        "metadata": _json_load(row["metadata_json"], None),
        "created_at": row["created_at"],
    }


async def create_run(
    session_id: str,
    user_prompt: str,
    *,
    panel_id: str | None = None,
    capability: str = "auto",
    run_id: str | None = None,
) -> dict[str, Any]:
    db = await history.get_db()
    now = _now()
    run_id = run_id or str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO agent_runs (
            id, session_id, panel_id, user_prompt, capability, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
        """,
        (run_id, session_id, panel_id, user_prompt, capability, now, now),
    )
    await db.commit()
    run = await get_run(run_id)
    if run is None:
        raise RuntimeError("created agent run was not found")
    return run


async def start_step(
    run_id: str,
    step_type: str,
    input_data: Any | None = None,
    *,
    step_id: str | None = None,
) -> dict[str, Any]:
    db = await history.get_db()
    step_id = step_id or str(uuid.uuid4())
    now = _now()
    row = await db.execute_fetchall(
        "SELECT COALESCE(MAX(step_index), 0) AS max_index FROM agent_steps WHERE run_id = ?",
        (run_id,),
    )
    step_index = int(row[0]["max_index"]) + 1
    await db.execute(
        """
        INSERT INTO agent_steps (
            id, run_id, step_index, step_type, status, input_json, started_at
        ) VALUES (?, ?, ?, ?, 'running', ?, ?)
        """,
        (step_id, run_id, step_index, step_type, _json_dump(input_data or {}), now),
    )
    await db.execute(
        """
        UPDATE agent_runs
        SET status = 'running', current_step_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (step_id, now, run_id),
    )
    await db.commit()
    step = await get_step(step_id)
    if step is None:
        raise RuntimeError("created agent step was not found")
    return step


async def complete_step(
    step_id: str,
    output_data: Any | None = None,
    *,
    status: str = "succeeded",
) -> dict[str, Any]:
    if status not in TERMINAL_STEP_STATUSES:
        raise ValueError(f"step completion status must be terminal, got {status}")
    db = await history.get_db()
    now = _now()
    cursor = await db.execute(
        """
        UPDATE agent_steps
        SET status = ?, output_json = ?, completed_at = ?
        WHERE id = ? AND status = 'running'
        """,
        (status, _json_dump(output_data or {}), now, step_id),
    )
    if cursor.rowcount == 1:
        row = await db.execute("SELECT run_id FROM agent_steps WHERE id = ?", (step_id,))
        step_row = await row.fetchone()
    else:
        step_row = None
    if step_row is not None:
        await db.execute(
            "UPDATE agent_runs SET updated_at = ? WHERE id = ?",
            (now, step_row["run_id"]),
        )
    await db.commit()
    step = await get_step(step_id)
    if step is None:
        raise RuntimeError("completed agent step was not found")
    return step


async def fail_step(
    step_id: str,
    error_data: Any,
    *,
    status: str = "failed",
    update_run_status: bool = True,
) -> dict[str, Any]:
    if status not in {"failed", "blocked", "cancelled"}:
        raise ValueError(f"step failure status must be failed, blocked, or cancelled, got {status}")
    db = await history.get_db()
    now = _now()
    cursor = await db.execute(
        """
        UPDATE agent_steps
        SET status = ?, error_json = ?, completed_at = ?
        WHERE id = ? AND status = 'running'
        """,
        (status, _json_dump(error_data), now, step_id),
    )
    if cursor.rowcount == 1:
        row = await db.execute("SELECT run_id FROM agent_steps WHERE id = ?", (step_id,))
        step_row = await row.fetchone()
    else:
        step_row = None
    if step_row is not None and update_run_status:
        await db.execute(
            "UPDATE agent_runs SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, step_row["run_id"]),
        )
    elif step_row is not None:
        await db.execute(
            "UPDATE agent_runs SET updated_at = ? WHERE id = ?",
            (now, step_row["run_id"]),
        )
    await db.commit()
    step = await get_step(step_id)
    if step is None:
        raise RuntimeError("failed agent step was not found")
    return step


async def mark_blocked_step_running(step_id: str) -> dict[str, Any]:
    db = await history.get_db()
    now = _now()
    cursor = await db.execute(
        """
        UPDATE agent_steps
        SET status = 'running', completed_at = NULL
        WHERE id = ? AND status = 'blocked'
        """,
        (step_id,),
    )
    claimed = cursor.rowcount == 1
    if claimed:
        row = await db.execute("SELECT run_id FROM agent_steps WHERE id = ?", (step_id,))
        step_row = await row.fetchone()
    else:
        step_row = None
    if step_row is not None:
        await db.execute(
            "UPDATE agent_runs SET status = 'running', current_step_id = ?, updated_at = ? WHERE id = ?",
            (step_id, now, step_row["run_id"]),
        )
    await db.commit()
    step = await get_step(step_id)
    if step is None:
        raise RuntimeError("agent step was not found")
    step["claimed"] = claimed
    return step


async def complete_run(
    run_id: str,
    *,
    status: str,
    result_request_id: str | None = None,
) -> dict[str, Any]:
    if status not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"run completion status must be terminal, got {status}")
    db = await history.get_db()
    now = _now()
    await db.execute(
        """
        UPDATE agent_runs
        SET status = ?, result_request_id = COALESCE(?, result_request_id),
            updated_at = ?, completed_at = ?
        WHERE id = ?
        """,
        (status, result_request_id, now, now, run_id),
    )
    await db.commit()
    run = await get_run(run_id)
    if run is None:
        raise RuntimeError("completed agent run was not found")
    return run


async def fail_run(
    run_id: str,
    error_data: Any | None = None,
    *,
    status: str = "failed",
) -> dict[str, Any]:
    if status not in {"failed", "blocked", "cancelled"}:
        raise ValueError(f"run failure status must be failed, blocked, or cancelled, got {status}")
    db = await history.get_db()
    now = _now()
    if error_data:
        cursor = await db.execute(
            "SELECT current_step_id FROM agent_runs WHERE id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        current_step_id = row["current_step_id"] if row else None
        if current_step_id:
            await db.execute(
                """
                UPDATE agent_steps
                SET status = ?, error_json = COALESCE(error_json, ?), completed_at = COALESCE(completed_at, ?)
                WHERE id = ? AND status = 'running'
                """,
                (status, _json_dump(error_data), now, current_step_id),
            )
    await db.execute(
        """
        UPDATE agent_runs
        SET status = ?, updated_at = ?, completed_at = COALESCE(completed_at, ?)
        WHERE id = ?
        """,
        (status, now, now, run_id),
    )
    await db.commit()
    run = await get_run(run_id)
    if run is None:
        raise RuntimeError("failed agent run was not found")
    return run


async def record_artifact(
    run_id: str,
    step_id: str,
    artifact_type: str,
    path: str,
    metadata: Any | None = None,
    *,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    db = await history.get_db()
    artifact_id = artifact_id or str(uuid.uuid4())
    now = _now()
    await db.execute(
        """
        INSERT INTO agent_artifacts (
            id, run_id, step_id, artifact_type, path, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (artifact_id, run_id, step_id, artifact_type, path, _json_dump(metadata or {}), now),
    )
    await db.commit()
    artifact = await get_artifact(artifact_id)
    if artifact is None:
        raise RuntimeError("created agent artifact was not found")
    return artifact


async def get_run(run_id: str) -> dict[str, Any] | None:
    db = await history.get_db()
    cursor = await db.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
    row = await cursor.fetchone()
    return _run_from_row(row) if row else None


async def get_step(step_id: str) -> dict[str, Any] | None:
    db = await history.get_db()
    cursor = await db.execute("SELECT * FROM agent_steps WHERE id = ?", (step_id,))
    row = await cursor.fetchone()
    return _step_from_row(row) if row else None


async def get_artifact(artifact_id: str) -> dict[str, Any] | None:
    db = await history.get_db()
    cursor = await db.execute("SELECT * FROM agent_artifacts WHERE id = ?", (artifact_id,))
    row = await cursor.fetchone()
    return _artifact_from_row(row) if row else None


async def list_steps(run_id: str) -> list[dict[str, Any]]:
    db = await history.get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY step_index",
        (run_id,),
    )
    return [_step_from_row(row) for row in rows]


async def list_artifacts(run_id: str) -> list[dict[str, Any]]:
    db = await history.get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM agent_artifacts WHERE run_id = ? ORDER BY created_at",
        (run_id,),
    )
    return [_artifact_from_row(row) for row in rows]


async def list_stale_running_runs(cutoff_iso: str) -> list[dict[str, Any]]:
    db = await history.get_db()
    rows = await db.execute_fetchall(
        """
        SELECT * FROM agent_runs
        WHERE status = 'running' AND updated_at < ?
        ORDER BY updated_at ASC
        """,
        (cutoff_iso,),
    )
    return [_run_from_row(row) for row in rows]


async def list_running_runs() -> list[dict[str, Any]]:
    db = await history.get_db()
    rows = await db.execute_fetchall(
        """
        SELECT * FROM agent_runs
        WHERE status = 'running'
        ORDER BY updated_at ASC
        """,
    )
    return [_run_from_row(row) for row in rows]


async def get_latest_resumable_run_for_panel(session_id: str, panel_id: str) -> dict[str, Any] | None:
    db = await history.get_db()
    cursor = await db.execute(
        """
        SELECT r.*
        FROM agent_runs r
        JOIN agent_steps s ON s.run_id = r.id
        WHERE r.session_id = ?
          AND r.panel_id = ?
          AND r.status = 'blocked'
          AND s.step_type = 'resume_available'
          AND s.status = 'blocked'
        ORDER BY r.updated_at DESC, r.created_at DESC
        LIMIT 1
        """,
        (session_id, panel_id),
    )
    row = await cursor.fetchone()
    return _run_from_row(row) if row else None


async def get_latest_resumable_run() -> dict[str, Any] | None:
    db = await history.get_db()
    cursor = await db.execute(
        """
        SELECT r.*
        FROM agent_runs r
        JOIN agent_steps s ON s.run_id = r.id
        WHERE r.status = 'blocked'
          AND s.step_type = 'resume_available'
          AND s.status = 'blocked'
        ORDER BY r.updated_at DESC, r.created_at DESC
        LIMIT 1
        """,
    )
    row = await cursor.fetchone()
    return _run_from_row(row) if row else None


async def get_latest_run_for_panel(session_id: str, panel_id: str) -> dict[str, Any] | None:
    db = await history.get_db()
    cursor = await db.execute(
        """
        SELECT * FROM agent_runs
        WHERE session_id = ? AND panel_id = ?
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        (session_id, panel_id),
    )
    row = await cursor.fetchone()
    return _run_from_row(row) if row else None
