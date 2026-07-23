"""Small transport validator and crash-safe execution journal.

Fusion's embedded Python environment does not install Backend dependencies, so
the Add-in validates the transport subset using the standard library.  The
Backend/Runtime remain the full Pydantic validation authority.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
MUTATING_ACTIONS = {
    "cad.update_parameter", "cad.update_feature_parameter", "cad.create_sketch",
    "cad.create_extrude", "cad.create_hole", "cad.create_fillet",
    "cad.create_chamfer", "cad.update_entity_properties", "cad.save_document", "cad.save_as",
}
OPERATIONS = {"context", "execute", "verify"}


class ProtocolError(RuntimeError):
    pass


class JournalSafetyError(RuntimeError):
    pass


def validate_task(raw: dict[str, Any]) -> dict[str, Any]:
    required = {
        "protocol_version", "request_id", "operation", "intent_hash", "lease_id",
        "attempt", "leased_until", "deadline", "payload", "execution_context",
    }
    if set(raw).difference(required):
        raise ProtocolError("task contains unknown transport fields")
    missing = required.difference(raw)
    if missing:
        raise ProtocolError("task is missing required fields")
    if raw["protocol_version"] != PROTOCOL_VERSION:
        raise ProtocolError("protocol version mismatch")
    if raw["operation"] not in OPERATIONS:
        raise ProtocolError("unsupported operation")
    try:
        uuid.UUID(str(raw["request_id"]))
        uuid.UUID(str(raw["lease_id"]))
    except ValueError as exc:
        raise ProtocolError("request_id and lease_id must be UUIDs") from exc
    if not isinstance(raw["payload"], dict) or not isinstance(raw["execution_context"], dict):
        raise ProtocolError("payload and execution_context must be objects")
    if raw["operation"] == "execute":
        action = raw["payload"].get("action")
        if action not in MUTATING_ACTIONS | {"cad.export"}:
            raise ProtocolError("unsupported action")
    return raw


def lease_identity(task: dict[str, Any], connector_instance_id: str) -> dict[str, Any]:
    return {
        "connector_instance_id": connector_instance_id,
        "request_id": task["request_id"],
        "lease_id": task["lease_id"],
        "attempt": task["attempt"],
        "intent_hash": task["intent_hash"],
    }


class ExecutionJournal:
    """Fail-closed mutation journal; corrupt state is never discarded."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._entries = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise JournalSafetyError("execution journal is unreadable or corrupt") from exc
        if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("entries"), dict):
            raise JournalSafetyError("execution journal has an unsupported structure")
        for request_id, entry in raw["entries"].items():
            if not isinstance(entry, dict) or entry.get("state") not in {"started", "completed"}:
                raise JournalSafetyError(f"execution journal entry {request_id} is invalid")
        return raw["entries"]

    def reconcile(self, task: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(str(task["request_id"]))
            if not entry:
                return None
            for field in ("intent_hash", "lease_id", "attempt"):
                if entry.get(field) != task.get(field):
                    raise JournalSafetyError(f"journal {field} does not match the task lease")
            if entry["state"] == "completed":
                return {"kind": "completed", "result": entry["result"]}
            return {
                "kind": "indeterminate",
                "result": indeterminate_result(task, "A prior mutation started but has no durable completed result"),
            }

    def record_started(self, task: dict[str, Any], snapshot: dict[str, Any]) -> None:
        entry = {
            "state": "started",
            "request_id": task["request_id"],
            "intent_hash": task["intent_hash"],
            "lease_id": task["lease_id"],
            "attempt": task["attempt"],
            "snapshot": snapshot,
        }
        with self._lock:
            existing = self._entries.get(str(task["request_id"]))
            if existing and existing != entry:
                raise JournalSafetyError("refusing to overwrite a different mutation journal entry")
            self._entries[str(task["request_id"])] = entry
            self._write()

    def record_completed(self, task: dict[str, Any], result: dict[str, Any]) -> None:
        with self._lock:
            entry = self._entries.get(str(task["request_id"]))
            if not entry or entry.get("state") != "started":
                raise JournalSafetyError("mutation completion has no matching started journal entry")
            for field in ("intent_hash", "lease_id", "attempt"):
                if entry.get(field) != task.get(field):
                    raise JournalSafetyError(f"completion {field} does not match started journal entry")
            entry = {**entry, "state": "completed", "result": result}
            self._entries[str(task["request_id"])] = entry
            self._write()

    def record_agent_report(self, request_id: str, report: dict[str, Any]) -> None:
        """Durably attach the exact public Cloud report to a completed mutation."""

        try:
            normalized_request_id = str(uuid.UUID(str(request_id)))
            encoded = json.dumps(
                report, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            )
            pure = json.loads(encoded)
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise JournalSafetyError("agent execution report is not finite JSON") from exc
        if not isinstance(pure, dict) or pure.get("request_id") != normalized_request_id:
            raise JournalSafetyError("agent execution report does not match the mutation request")
        with self._lock:
            entry = self._entries.get(normalized_request_id)
            if not entry or entry.get("state") != "completed":
                raise JournalSafetyError("agent execution report has no completed mutation")
            existing = entry.get("agent_report")
            if existing is not None and existing != pure:
                raise JournalSafetyError("refusing to replace a different agent execution report")
            entry = {**entry, "agent_report_state": "pending", "agent_report": pure}
            self._entries[normalized_request_id] = entry
            self._write()

    def pending_agent_reports(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                json.loads(json.dumps(entry["agent_report"]))
                for entry in self._entries.values()
                if entry.get("state") == "completed"
                and entry.get("agent_report_state") == "pending"
                and isinstance(entry.get("agent_report"), dict)
            ]

    def mark_agent_reported(self, request_id: str, report_id: str) -> None:
        normalized_request_id = str(uuid.UUID(str(request_id)))
        normalized_report_id = str(uuid.UUID(str(report_id)))
        with self._lock:
            entry = self._entries.get(normalized_request_id)
            report = entry.get("agent_report") if isinstance(entry, dict) else None
            if (
                not isinstance(report, dict)
                or report.get("report_id") != normalized_report_id
                or entry.get("agent_report_state") != "pending"
            ):
                raise JournalSafetyError("agent report receipt does not match the pending report")
            entry = {**entry, "agent_report_state": "reported"}
            entry.pop("agent_report", None)
            self._entries[normalized_request_id] = entry
            self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        content = json.dumps({"version": 1, "entries": self._entries}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            if hasattr(os, "O_DIRECTORY"):
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as exc:
            raise JournalSafetyError("failed to durably write execution journal") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def indeterminate_result(task: dict[str, Any], message: str) -> dict[str, Any]:
    action = task.get("payload", {}).get("action", "cad.unknown")
    return {
        "request_id": task["request_id"],
        "status": "indeterminate",
        "action": action,
        "data": None,
        "changes": [],
        "warnings": ["Mutation was not replayed; explicit reconciliation is required"],
        "verification": None,
        "artifacts": [],
        "approval": None,
        "error": {
            "code": "VERIFICATION_FAILED", "message": message,
            "category": "verification", "retryable": False, "details": {},
        },
    }
