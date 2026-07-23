"""Main-thread-only allowlisted operation dispatcher."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from .protocol import ExecutionJournal, JournalSafetyError, MUTATING_ACTIONS, indeterminate_result

log = logging.getLogger("CADAgentFusionConnector.dispatcher")

_ERROR_CATEGORIES = {
    "REQUEST_CANCELLED": "cancel",
    "STALE_LEASE": "conflict",
    "DOCUMENT_MISMATCH": "conflict",
    "TARGET_AMBIGUOUS": "conflict",
    "TARGET_NOT_FOUND": "not_found",
    "PARAMETER_NOT_FOUND": "not_found",
    "FEATURE_NOT_FOUND": "not_found",
    "NO_ACTIVE_DOCUMENT": "not_found",
    "NO_ACTIVE_DESIGN": "not_found",
    "DOCUMENT_READ_ONLY": "read_only",
    "DOCUMENT_UNSAVED": "validation",
    "INVALID_ACTION": "validation",
    "UNSUPPORTED_ACTION": "validation",
    "COMPUTE_FAILED": "fusion",
    "FUSION_API_ERROR": "fusion",
    "VERIFICATION_FAILED": "verification",
    "EXPORT_FAILED": "artifact",
    "ARTIFACT_INVALID": "artifact",
    "PATH_NOT_ALLOWED": "artifact",
}


class CancelToken:
    def __init__(self):
        self.cancel_requested = False
        self.lease_valid = True

    def check(self) -> None:
        if self.cancel_requested:
            raise DispatchError("REQUEST_CANCELLED", "Fusion request was cancelled")
        if not self.lease_valid:
            raise DispatchError("STALE_LEASE", "Fusion task lease is no longer valid")


class DispatchError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class Dispatcher:
    def __init__(self, facade: Any, journal: ExecutionJournal):
        self.facade = facade
        self.journal = journal

    def dispatch(self, task: dict[str, Any], cancel: CancelToken) -> dict[str, Any]:
        try:
            cancel.check()
            operation = task["operation"]
            if operation == "context":
                return self._bind_result(task, self.facade.get_context(task["payload"], cancel))
            if operation == "verify":
                return self._bind_result(task, self.facade.verify(task["payload"], cancel))
            if operation != "execute":
                raise DispatchError("UNSUPPORTED_ACTION", f"Unsupported operation: {operation}")
            action = task["payload"]["action"]
            if action not in MUTATING_ACTIONS | {"cad.export"}:
                raise DispatchError("UNSUPPORTED_ACTION", f"Unsupported action: {action}")
            if task["payload"].get("execution_mode", "execute") == "preview":
                return self._bind_result(task, self.facade.preview(task["payload"], cancel))
            if action == "cad.export":
                return self._bind_result(
                    task,
                    self.facade.execute(task["payload"], task["execution_context"], cancel),
                )
            return self._mutation(task, cancel)
        except JournalSafetyError as exc:
            return indeterminate_result(task, str(exc))
        except DispatchError as exc:
            return failure_result(task, exc.code, exc.message, exc.details)
        except Exception:
            # Keep diagnostic detail local; public results intentionally contain no
            # traceback, secret, absolute path, or raw exception string.
            log.exception(
                "Fusion operation failed",
                extra={"request_id": task.get("request_id"), "operation": task.get("operation")},
            )
            return failure_result(task, "FUSION_API_ERROR", "Fusion operation failed", {})

    def _mutation(self, task: dict[str, Any], cancel: CancelToken) -> dict[str, Any]:
        reconciled = self.journal.reconcile(task)
        if reconciled:
            return reconciled["result"]
        cancel.check()
        snapshot = self.facade.snapshot(task["payload"])
        snapshot.setdefault("snapshot_id", str(uuid.uuid4()))
        self.journal.record_started(task, snapshot)
        cancel.check()
        try:
            action = {
                **task["payload"],
                "request_id": task["request_id"],
                "_snapshot": snapshot,
            }
            result = self.facade.execute(action, task["execution_context"], cancel)
            cancel.check()
        except Exception:
            try:
                self.facade.compensate(task["payload"], snapshot)
            except Exception:
                return indeterminate_result(task, "Fusion mutation failed and compensation could not be verified")
            raise
        result = self._bind_result(task, result)
        data = result.get("data")
        if isinstance(data, dict) and data.get("kind") == "mutation":
            data["snapshot_id"] = snapshot["snapshot_id"]
            result["_snapshot"] = snapshot
        self.journal.record_completed(task, result)
        return result

    @staticmethod
    def _bind_result(task: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """Bind public identity to the leased task, never facade-supplied input."""
        bound = dict(result)
        bound["request_id"] = task["request_id"]
        if task["operation"] == "context":
            bound["action"] = "cad.get_context"
        elif task["operation"] == "verify":
            bound["action"] = "cad.verify"
        else:
            bound["action"] = task.get("payload", {}).get("action", "cad.unknown")
        return bound


def failure_result(task: dict[str, Any], code: str, message: str, details: dict[str, Any]) -> dict[str, Any]:
    category = _ERROR_CATEGORIES.get(code, "fusion")
    return {
        "request_id": task["request_id"], "status": "cancelled" if code == "REQUEST_CANCELLED" else "failed",
        "action": task.get("payload", {}).get("action", "cad.unknown"), "data": None,
        "changes": [], "warnings": [], "verification": None, "artifacts": [], "approval": None,
        "error": {"code": code, "message": message, "category": category, "retryable": False, "details": details},
    }
