"""Durable SQLite state machine for the local Fusion connector Runtime."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contract import PROTOCOL_VERSION
from .errors import FusionConnectorError

TERMINAL_STATES = {
    "success", "failed", "cancelled", "timeout", "approval_required", "offline", "indeterminate",
}
MUTATING_ACTIONS = {
    "cad.update_parameter", "cad.update_feature_parameter", "cad.create_sketch",
    "cad.create_extrude", "cad.create_hole", "cad.create_fillet",
    "cad.create_chamfer", "cad.update_entity_properties", "cad.save_document", "cad.save_as",
}


class RuntimeStore:
    def __init__(self, path: str | Path, *, max_queue: int = 1_000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_queue = min(max(1, max_queue), 1_000)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS connectors (
                    connector_instance_id TEXT PRIMARY KEY,
                    protocol_version INTEGER NOT NULL,
                    fusion_version TEXT NOT NULL,
                    addin_version TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    user_name TEXT,
                    artifact_root_fingerprint TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    last_heartbeat REAL NOT NULL,
                    registered_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    request_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    action_name TEXT NOT NULL,
                    intent_hash TEXT NOT NULL,
                    idempotency_key TEXT,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    is_mutation INTEGER NOT NULL,
                    connector_instance_id TEXT,
                    lease_id TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    leased_until REAL,
                    deadline REAL NOT NULL,
                    started_at REAL,
                    result_json TEXT,
                    error_json TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(owner_id, idempotency_key, phase)
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(status, connector_instance_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_id, created_at);
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    connector_instance_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    intent_hash TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL,
                    consumed_request_id TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )

    def register_connector(self, registration: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        common = set(registration.get("protocol_versions", [])).intersection({PROTOCOL_VERSION})
        if not common:
            raise FusionConnectorError("PROTOCOL_MISMATCH", "No compatible Fusion connector protocol version")
        selected = max(common)
        values = (
            str(registration["connector_instance_id"]), selected, registration["fusion_version"],
            registration["addin_version"], registration["platform"], registration.get("user_name"),
            registration["artifact_root_fingerprint"], _dump(registration["capabilities"]), now, now,
        )
        with self._lock:
            self._db.execute(
                """INSERT INTO connectors VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(connector_instance_id) DO UPDATE SET
                    protocol_version=excluded.protocol_version,
                    fusion_version=excluded.fusion_version,
                    addin_version=excluded.addin_version,
                    platform=excluded.platform,
                    user_name=excluded.user_name,
                    artifact_root_fingerprint=excluded.artifact_root_fingerprint,
                    capabilities_json=excluded.capabilities_json,
                    last_heartbeat=excluded.last_heartbeat""",
                values,
            )
        return {"protocol_version": selected, "heartbeat_interval_ms": 5_000, "heartbeat_ttl_ms": 15_000, "lease_duration_ms": 30_000}

    def heartbeat(self, connector_id: str, *, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            cursor = self._db.execute(
                "UPDATE connectors SET last_heartbeat=? WHERE connector_instance_id=?",
                (now, connector_id),
            )
            if cursor.rowcount != 1:
                raise FusionConnectorError("CONNECTOR_OFFLINE", "Connector is not registered")

    def connectors(self, *, now: float | None = None, ttl_s: float = 15.0) -> list[dict[str, Any]]:
        now = now or time.time()
        with self._lock:
            rows = self._db.execute("SELECT * FROM connectors ORDER BY registered_at").fetchall()
        return [
            {
                **dict(row),
                "capabilities": _load(row["capabilities_json"]),
                "online": now - row["last_heartbeat"] <= ttl_s,
            }
            for row in rows
        ]

    def select_connector(self, requested: str | None = None, *, now: float | None = None) -> dict[str, Any]:
        online = [c for c in self.connectors(now=now) if c["online"]]
        if requested:
            matches = [c for c in online if c["connector_instance_id"] == requested]
            if not matches:
                raise FusionConnectorError("CONNECTOR_OFFLINE", "Requested Fusion connector is offline")
            return matches[0]
        if not online:
            raise FusionConnectorError("CONNECTOR_OFFLINE", "No Fusion connector is online")
        if len(online) > 1:
            raise FusionConnectorError("CONNECTOR_SELECTION_REQUIRED", "Select a Fusion connector instance")
        return online[0]

    def submit_task(
        self,
        *,
        request_id: str,
        owner_id: str,
        operation: str,
        phase: str,
        action_name: str,
        intent_hash: str,
        idempotency_key: str | None,
        payload_hash: str,
        payload: dict[str, Any],
        connector_instance_id: str,
        deadline: float,
        approval: dict[str, str] | None = None,
        now: float | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = now or time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key:
                    existing = self._db.execute(
                        "SELECT * FROM tasks WHERE owner_id=? AND idempotency_key=? AND phase=?",
                        (owner_id, idempotency_key, phase),
                    ).fetchone()
                    if existing:
                        if existing["payload_hash"] != payload_hash:
                            raise FusionConnectorError("IDEMPOTENCY_CONFLICT", "Idempotency key was used with a different payload")
                        self._db.execute("COMMIT")
                        return self._task_dict(existing), True
                same_request = self._db.execute(
                    "SELECT * FROM tasks WHERE request_id=?", (request_id,)
                ).fetchone()
                if same_request:
                    if same_request["owner_id"] != owner_id or same_request["payload_hash"] != payload_hash:
                        raise FusionConnectorError("IDEMPOTENCY_CONFLICT", "Request ID was reused with a different payload")
                    self._db.execute("COMMIT")
                    return self._task_dict(same_request), True
                queued = self._db.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','leased','running')"
                ).fetchone()[0]
                if queued >= self.max_queue:
                    raise FusionConnectorError("QUEUE_FULL", "Fusion Runtime task queue is full")
                if approval:
                    self._consume_approval_in_transaction(now=now, **approval)
                self._db.execute(
                    """INSERT INTO tasks (
                        request_id,owner_id,operation,phase,action_name,intent_hash,idempotency_key,
                        payload_hash,payload_json,status,is_mutation,connector_instance_id,deadline,
                        created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?)""",
                    (
                        request_id, owner_id, operation, phase, action_name, intent_hash,
                        idempotency_key, payload_hash, _dump(payload), int(phase == "execute" and action_name in MUTATING_ACTIONS),
                        connector_instance_id, deadline, now, now,
                    ),
                )
                row = self._db.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
                self._db.execute("COMMIT")
                return self._task_dict(row), False
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def find_idempotent(
        self, owner_id: str, idempotency_key: str | None, phase: str, payload_hash: str
    ) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM tasks WHERE owner_id=? AND idempotency_key=? AND phase=?",
                (owner_id, idempotency_key, phase),
            ).fetchone()
        if not row:
            return None
        if row["payload_hash"] != payload_hash:
            raise FusionConnectorError("IDEMPOTENCY_CONFLICT", "Idempotency key was used with a different payload")
        return self._task_dict(row)

    def lease_next(self, connector_id: str, *, lease_s: float = 30.0, now: float | None = None) -> dict[str, Any] | None:
        now = now or time.time()
        self.reap(now=now)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                # After an Add-in restart, return the same in-flight lease.  The
                # durable Add-in journal decides completed-resend vs indeterminate;
                # Runtime never creates a second mutation attempt here.
                existing = self._db.execute(
                    """SELECT * FROM tasks WHERE connector_instance_id=?
                    AND status IN ('leased','running') ORDER BY started_at DESC, created_at LIMIT 1""",
                    (connector_id,),
                ).fetchone()
                if existing:
                    self._db.execute("COMMIT")
                    return self._task_dict(existing)
                running_mutation = self._db.execute(
                    "SELECT 1 FROM tasks WHERE connector_instance_id=? AND is_mutation=1 AND status IN ('leased','running') LIMIT 1",
                    (connector_id,),
                ).fetchone()
                if running_mutation:
                    predicate = "AND is_mutation=0"
                else:
                    predicate = ""
                row = self._db.execute(
                    f"""SELECT * FROM tasks WHERE status='queued' AND connector_instance_id=?
                    AND deadline>? {predicate} ORDER BY is_mutation DESC, created_at LIMIT 1""",
                    (connector_id, now),
                ).fetchone()
                if not row:
                    self._db.execute("COMMIT")
                    return None
                lease_id = str(uuid.uuid4())
                attempt = row["attempt"] + 1
                leased_until = min(now + lease_s, row["deadline"])
                self._db.execute(
                    """UPDATE tasks SET status='leased',lease_id=?,attempt=?,leased_until=?,updated_at=?
                    WHERE request_id=? AND status='queued'""",
                    (lease_id, attempt, leased_until, now, row["request_id"]),
                )
                leased = self._db.execute("SELECT * FROM tasks WHERE request_id=?", (row["request_id"],)).fetchone()
                self._db.execute("COMMIT")
                return self._task_dict(leased)
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def mark_started(self, connector_id: str, request_id: str, lease_id: str, attempt: int, intent_hash: str, *, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        with self._lock:
            cursor = self._db.execute(
                """UPDATE tasks SET status='running',started_at=?,updated_at=?
                WHERE request_id=? AND connector_instance_id=? AND lease_id=? AND attempt=?
                AND intent_hash=? AND status='leased' AND leased_until>=?""",
                (now, now, request_id, connector_id, lease_id, attempt, intent_hash, now),
            )
            if cursor.rowcount != 1:
                current = self._db.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
                if not (
                    current and current["status"] == "running"
                    and current["connector_instance_id"] == connector_id
                    and current["lease_id"] == lease_id and current["attempt"] == attempt
                    and current["intent_hash"] == intent_hash
                ):
                    raise FusionConnectorError("STALE_LEASE", "Task lease is no longer valid")
        return self.get_task_internal(request_id)

    def complete_task(
        self,
        connector_id: str,
        request_id: str,
        lease_id: str,
        attempt: int,
        intent_hash: str,
        result: dict[str, Any],
        *,
        approval: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = now or time.time()
        self.reap(now=now)
        stored_result = dict(result)
        if approval:
            stored_result["status"] = "approval_required"
            stored_result["approval"] = {
                "approval_id": approval["approval_id"],
                "intent_hash": approval["intent_hash"],
                "risk": approval["risk"],
                "expires_at": datetime.fromtimestamp(approval["expires_at"], tz=timezone.utc).isoformat(),
                "connector_instance_id": approval["connector_instance_id"],
                "document_id": approval["document_id"],
            }
        status = stored_result.get("status", "failed")
        if status not in TERMINAL_STATES:
            status = "failed"
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                current = self._db.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
                if current and current["status"] in TERMINAL_STATES and current["result_json"]:
                    if current["lease_id"] == lease_id and current["attempt"] == attempt and current["intent_hash"] == intent_hash:
                        self._db.execute("COMMIT")
                        return self._task_dict(current)
                valid = bool(
                    current
                    and current["connector_instance_id"] == connector_id
                    and current["lease_id"] == lease_id
                    and current["attempt"] == attempt
                    and current["intent_hash"] == intent_hash
                    and current["status"] in {"leased", "running"}
                )
                if not valid:
                    raise FusionConnectorError("STALE_LEASE", "Task result has a stale lease")
                if approval:
                    self._db.execute(
                        "INSERT INTO approvals VALUES (?,?,?,?,?,?,?,NULL,NULL,?)",
                        (
                            approval["approval_id"], approval["owner_id"],
                            approval["connector_instance_id"], approval["document_id"],
                            approval["intent_hash"], approval["risk"],
                            approval["expires_at"], now,
                        ),
                    )
                if snapshot:
                    self._db.execute(
                        "INSERT INTO snapshots VALUES (?,?,?,?,?)",
                        (
                            snapshot["snapshot_id"], snapshot["owner_id"],
                            snapshot["request_id"], _dump(snapshot["payload"]), now,
                        ),
                    )
                cursor = self._db.execute(
                    """UPDATE tasks SET status=?,result_json=?,updated_at=?
                    WHERE request_id=? AND connector_instance_id=? AND lease_id=? AND attempt=?
                    AND intent_hash=? AND status IN ('leased','running')""",
                    (status, _dump(stored_result), now, request_id, connector_id, lease_id, attempt, intent_hash),
                )
                if cursor.rowcount != 1:
                    raise FusionConnectorError("STALE_LEASE", "Task result has a stale lease")
                completed = self._db.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
                self._db.execute("COMMIT")
                return self._task_dict(completed)
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def assert_active_lease(
        self,
        connector_id: str,
        request_id: str,
        lease_id: str,
        attempt: int,
        intent_hash: str,
        *,
        now: float | None = None,
    ) -> None:
        """Fence result processing before artifact or approval side effects."""
        now = now or time.time()
        self.reap(now=now)
        with self._lock:
            current = self._db.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
        valid = bool(
            current
            and current["connector_instance_id"] == connector_id
            and current["lease_id"] == lease_id
            and current["attempt"] == attempt
            and current["intent_hash"] == intent_hash
            and current["status"] in {"leased", "running"}
        )
        if not valid:
            raise FusionConnectorError("STALE_LEASE", "Task result has a stale lease")

    def get_task_internal(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            raise FusionConnectorError("SOURCE_RESULT_NOT_FOUND", "Fusion request was not found")
        return self._task_dict(row)

    def get_task(self, owner_id: str, request_id: str) -> dict[str, Any]:
        task = self.get_task_internal(request_id)
        if task["owner_id"] != owner_id:
            raise FusionConnectorError("OWNER_MISMATCH", "Fusion request was not found")
        return task

    def request_cancel(self, owner_id: str, request_id: str, *, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        task = self.get_task(owner_id, request_id)
        with self._lock:
            if task["status"] == "queued":
                self._db.execute(
                    "UPDATE tasks SET status='cancelled',cancel_requested=1,updated_at=? WHERE request_id=?",
                    (now, request_id),
                )
            elif task["status"] not in TERMINAL_STATES:
                self._db.execute(
                    "UPDATE tasks SET cancel_requested=1,updated_at=? WHERE request_id=?",
                    (now, request_id),
                )
        return self.get_task(owner_id, request_id)

    def control(self, connector_id: str, request_id: str, lease_id: str, attempt: int, *, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        task = self.get_task_internal(request_id)
        valid = (
            task["connector_instance_id"] == connector_id and task["lease_id"] == lease_id
            and task["attempt"] == attempt and task["status"] in {"leased", "running"}
        )
        return {"cancel_requested": bool(task["cancel_requested"]), "deadline": task["deadline"], "lease_valid": valid and now <= task["deadline"]}

    def reap(self, *, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET status='timeout',updated_at=? WHERE status='queued' AND deadline<?",
                (now, now),
            )
            self._db.execute(
                """UPDATE tasks SET status='indeterminate',updated_at=?
                WHERE status='running' AND is_mutation=1 AND deadline<?""",
                (now, now),
            )
            self._db.execute(
                """UPDATE tasks SET status='timeout',updated_at=?
                WHERE status='running' AND is_mutation=0 AND deadline<?""",
                (now, now),
            )
            self._db.execute(
                """UPDATE tasks SET status='queued',lease_id=NULL,leased_until=NULL,updated_at=?
                WHERE status='leased' AND started_at IS NULL AND leased_until<? AND deadline>=?""",
                (now, now, now),
            )
            self._db.execute(
                """UPDATE tasks SET status='indeterminate',updated_at=?
                WHERE status='running' AND is_mutation=1 AND connector_instance_id IN (
                    SELECT connector_instance_id FROM connectors WHERE last_heartbeat<?
                )""",
                (now, now - 15.0),
            )

    def create_approval(
        self,
        *,
        owner_id: str,
        connector_instance_id: str,
        document_id: str,
        intent_hash: str,
        risk: str,
        ttl_s: float = 600,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = now or time.time()
        approval_id = str(uuid.uuid4())
        with self._lock:
            self._db.execute(
                "INSERT INTO approvals VALUES (?,?,?,?,?,?,?,NULL,NULL,?)",
                (approval_id, owner_id, connector_instance_id, document_id, intent_hash, risk, now + ttl_s, now),
            )
        return {
            "approval_id": approval_id,
            "intent_hash": intent_hash,
            "risk": risk,
            "expires_at": now + ttl_s,
            "connector_instance_id": connector_instance_id,
            "document_id": document_id,
        }

    def consume_approval(
        self,
        approval_id: str,
        *,
        owner_id: str,
        connector_instance_id: str,
        document_id: str,
        intent_hash: str,
        request_id: str,
        now: float | None = None,
    ) -> None:
        now = now or time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._consume_approval_in_transaction(
                    approval_id=approval_id,
                    owner_id=owner_id,
                    connector_instance_id=connector_instance_id,
                    document_id=document_id,
                    intent_hash=intent_hash,
                    request_id=request_id,
                    now=now,
                )
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def _consume_approval_in_transaction(
        self,
        *,
        approval_id: str,
        owner_id: str,
        connector_instance_id: str,
        document_id: str,
        intent_hash: str,
        request_id: str,
        now: float,
    ) -> None:
        """Validate and consume an approval inside the caller's write transaction."""
        row = self._db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if not row:
            raise FusionConnectorError("APPROVAL_INVALID", "Approval does not exist")
        if row["owner_id"] != owner_id:
            raise FusionConnectorError("OWNER_MISMATCH", "Approval does not exist")
        if row["consumed_at"] is not None:
            raise FusionConnectorError("APPROVAL_ALREADY_USED", "Approval was already consumed")
        if row["expires_at"] < now:
            raise FusionConnectorError("APPROVAL_EXPIRED", "Approval has expired")
        if any((
            row["connector_instance_id"] != connector_instance_id,
            row["document_id"] != document_id,
            row["intent_hash"] != intent_hash,
        )):
            raise FusionConnectorError("APPROVAL_INVALID", "Approval does not match this action")
        cursor = self._db.execute(
            "UPDATE approvals SET consumed_at=?,consumed_request_id=? WHERE approval_id=? AND consumed_at IS NULL",
            (now, request_id, approval_id),
        )
        if cursor.rowcount != 1:
            raise FusionConnectorError("APPROVAL_ALREADY_USED", "Approval was already consumed")

    def put_snapshot(self, snapshot_id: str, owner_id: str, request_id: str, payload: dict[str, Any], *, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?)",
                (snapshot_id, owner_id, request_id, _dump(payload), now),
            )

    def get_snapshot(self, owner_id: str, snapshot_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if not row or row["owner_id"] != owner_id:
            raise FusionConnectorError("BASELINE_NOT_FOUND", "Fusion snapshot was not found")
        return _load(row["payload_json"])

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["payload"] = _load(value.pop("payload_json"))
        value["result"] = _load(value.pop("result_json")) if value.get("result_json") else None
        value["error"] = _load(value.pop("error_json")) if value.get("error_json") else None
        value["is_mutation"] = bool(value["is_mutation"])
        value["cancel_requested"] = bool(value["cancel_requested"])
        return value


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(value: str) -> Any:
    return json.loads(value)
