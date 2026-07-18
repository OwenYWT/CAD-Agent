"""Async orchestration over the durable Fusion Runtime store."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from .artifacts import ArtifactStore
from .contract import CAD_ACTION_ADAPTER, ContextRequest, TaskResultEnvelope, VerifyRequest
from .errors import FusionConnectorError
from .policy import (
    action_intent_hash,
    classify_risk,
    idempotency_payload_hash,
    requires_approval,
    sha256_canonical,
)
from .runtime_store import RuntimeStore, TERMINAL_STATES


class RuntimeService:
    def __init__(self, store: RuntimeStore, artifacts: ArtifactStore):
        self.store = store
        self.artifacts = artifacts

    async def submit(self, owner_id: str, operation: str, raw_payload: dict[str, Any], *, wait: bool = True) -> dict[str, Any]:
        if operation == "execute":
            model = CAD_ACTION_ADAPTER.validate_python(raw_payload)
            payload = model.model_dump(mode="json")
            request_id = str(model.request_id)
            connector_requested = str(model.connector_instance_id) if model.connector_instance_id else None
            phase = model.execution_mode
            action_name = model.action
            intent = action_intent_hash(model)
            idempotency_key = model.idempotency_key
            timeout_ms = model.timeout_ms
            document_id = model.target.document_id
        elif operation == "context":
            model = ContextRequest.model_validate(raw_payload)
            payload = model.model_dump(mode="json")
            request_id = str(model.request_id)
            connector_requested = str(model.query.connector_instance_id) if model.query.connector_instance_id else None
            phase = "execute"
            action_name = "cad.get_context"
            intent = sha256_canonical(payload)
            idempotency_key = None
            timeout_ms = model.timeout_ms
            document_id = "active"
        elif operation == "verify":
            model = VerifyRequest.model_validate(raw_payload)
            payload = model.model_dump(mode="json")
            request_id = str(model.request_id)
            connector_requested = str(model.connector_instance_id) if model.connector_instance_id else None
            phase = "execute"
            action_name = "cad.verify"
            intent = sha256_canonical(payload)
            idempotency_key = None
            timeout_ms = model.timeout_ms
            document_id = model.specification.document_id
            references = await asyncio.to_thread(self._verify_references, owner_id, model)
            if references:
                payload["_references"] = references
        else:
            raise FusionConnectorError("UNSUPPORTED_ACTION", f"Unsupported Runtime operation: {operation}")

        connector = await asyncio.to_thread(self.store.select_connector, connector_requested)
        connector_id = connector["connector_instance_id"]
        deadline = time.time() + timeout_ms / 1000
        payload_hash = idempotency_payload_hash(owner_id, phase, intent)
        replay_task = await asyncio.to_thread(
            self.store.find_idempotent, owner_id, idempotency_key, phase, payload_hash
        )
        if replay_task:
            if not wait:
                return self.public_task(replay_task)
            return await self.wait(owner_id, replay_task["request_id"], deadline=replay_task["deadline"])

        approval: dict[str, str] | None = None
        if operation == "execute" and phase == "execute" and requires_approval(action_name):
            if not model.approval_id:
                raise FusionConnectorError("APPROVAL_REQUIRED", "This Fusion action requires an approved preview")
            approval = {
                "approval_id": str(model.approval_id),
                "owner_id": owner_id,
                "connector_instance_id": connector_id,
                "document_id": document_id,
                "intent_hash": intent,
                "request_id": request_id,
            }

        task, replay = await asyncio.to_thread(
            self.store.submit_task,
            request_id=request_id,
            owner_id=owner_id,
            operation=operation,
            phase=phase,
            action_name=action_name,
            intent_hash=intent,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            payload=payload,
            connector_instance_id=connector_id,
            deadline=deadline,
            approval=approval,
        )
        if not wait:
            return self.public_task(task)
        return await self.wait(owner_id, task["request_id"], deadline=task["deadline"])

    def _verify_references(self, owner_id: str, model: VerifyRequest) -> dict[str, Any]:
        specification = model.specification
        references: dict[str, Any] = {}
        if specification.baseline_request_id:
            baseline = self.store.get_task(owner_id, str(specification.baseline_request_id))
            result = baseline.get("result") or {}
            snapshot_id = (result.get("data") or {}).get("snapshot_id")
            if snapshot_id:
                snapshot = self.store.get_snapshot(owner_id, snapshot_id)
                self._assert_snapshot_document(snapshot, specification.document_id)
                references["baseline_feature_errors"] = snapshot.get("feature_errors", [])
            else:
                references["baseline_feature_errors"] = (
                    result.get("verification") or {}
                ).get("new_feature_errors", [])
        snapshot_feature_errors: dict[str, list[str]] = {}
        for check in specification.checks:
            snapshot_id = getattr(check, "snapshot_id", None)
            if not snapshot_id:
                continue
            snapshot = self.store.get_snapshot(owner_id, snapshot_id)
            self._assert_snapshot_document(snapshot, specification.document_id)
            snapshot_feature_errors[snapshot_id] = list(snapshot.get("feature_errors", []))
        if snapshot_feature_errors:
            references["snapshot_feature_errors"] = snapshot_feature_errors
        if specification.source_request_id:
            source = self.store.get_task(owner_id, str(specification.source_request_id))
            source_result = source.get("result") or {}
            references["source_result"] = source_result
            artifact_evidence: dict[str, dict[str, Any]] = {}
            for check in specification.checks:
                if check.check != "artifact_valid":
                    continue
                artifact_id = str(check.artifact_id)
                match = next(
                    (item for item in source_result.get("artifacts", []) if item.get("artifact_id") == artifact_id),
                    None,
                )
                evidence: dict[str, Any] = {"valid": False}
                if match:
                    try:
                        path = self.artifacts.resolve_local(
                            owner_id, str(specification.source_request_id), match["filename"]
                        )
                        size, sha256 = self.artifacts.validate_file(path, match["kind"])
                        evidence = {
                            "valid": size == match["size_bytes"] and sha256 == match["sha256"],
                            "size_bytes": size,
                            "sha256": sha256,
                        }
                    except FusionConnectorError:
                        evidence = {"valid": False}
                artifact_evidence[artifact_id] = evidence
            if artifact_evidence:
                references["artifact_evidence"] = artifact_evidence
        return references

    @staticmethod
    def _assert_snapshot_document(snapshot: dict[str, Any], document_id: str) -> None:
        if snapshot.get("document_id") != document_id:
            raise FusionConnectorError(
                "DOCUMENT_MISMATCH",
                "Fusion snapshot belongs to a different document",
            )

    async def wait(self, owner_id: str, request_id: str, *, deadline: float) -> dict[str, Any]:
        while True:
            task = await asyncio.to_thread(self.store.get_task, owner_id, request_id)
            if task["status"] in TERMINAL_STATES or task["result"] is not None:
                return self.public_task(task)
            if time.time() >= deadline:
                await asyncio.to_thread(self.store.reap)
                task = await asyncio.to_thread(self.store.get_task, owner_id, request_id)
                return self.public_task(task)
            await asyncio.sleep(0.05)

    async def record_result(self, envelope: TaskResultEnvelope) -> dict[str, Any]:
        request_id = str(envelope.request_id)
        task = await asyncio.to_thread(self.store.get_task_internal, request_id)
        if task["result"] is not None:
            return self.public_task(task)
        connector_id = str(envelope.connector_instance_id)
        if task["connector_instance_id"] != connector_id:
            raise FusionConnectorError("STALE_LEASE", "Task belongs to a different connector")
        await asyncio.to_thread(
            self.store.assert_active_lease,
            connector_id,
            request_id,
            str(envelope.lease_id),
            envelope.attempt,
            envelope.intent_hash,
        )
        result = dict(envelope.result)
        # The outer lease identity is authoritative. A connector result cannot
        # redirect or relabel the durable task it is completing.
        result["request_id"] = request_id
        result["action"] = task["action_name"]
        # Snapshot evidence has its own executor-only envelope field and is
        # never accepted from or retained in the public result object.
        result.pop("_snapshot", None)
        snapshot_evidence = envelope.snapshot_evidence
        if task["phase"] == "preview":
            # Preview results describe a future verification plan; no model
            # rebuild was performed, regardless of what an older Add-in claims.
            result["verification"] = None
        artifacts = []
        for local in envelope.local_artifacts:
            artifact = await asyncio.to_thread(
                self.artifacts.accept_local,
                task["owner_id"],
                request_id,
                local,
            )
            artifacts.append(artifact.model_dump(mode="json"))
        result["artifacts"] = artifacts

        if (
            task["phase"] == "execute"
            and task["action_name"] == "cad.export"
            and result.get("status") == "success"
        ):
            data = result.get("data") or {}
            declared_ids = set(data.get("artifact_ids", [])) if isinstance(data, dict) else set()
            observed_ids = {item["artifact_id"] for item in artifacts}
            if not observed_ids or declared_ids != observed_ids:
                result = self._artifact_failure(request_id, task["action_name"], result)

        if task["phase"] == "execute" and result.get("status") == "success":
            data = result.get("data")
            expected_kind = (
                "save" if task["action_name"] in {"cad.save_document", "cad.save_as"}
                else "export" if task["action_name"] == "cad.export"
                else "mutation" if task["is_mutation"]
                else None
            )
            if expected_kind and (not isinstance(data, dict) or data.get("kind") != expected_kind):
                result = self._verification_failure(request_id, task["action_name"], result)
            elif expected_kind and not self._has_complete_verification(result):
                result = self._verification_failure(request_id, task["action_name"], result)
            elif expected_kind == "mutation" and not result.get("changes"):
                result = self._verification_failure(request_id, task["action_name"], result)
            elif expected_kind == "save" and not data.get("local_save_accepted"):
                result = self._verification_failure(request_id, task["action_name"], result)

        snapshot = None
        data = result.get("data") or {}
        if (
            result.get("status") == "success"
            and isinstance(data, dict)
            and data.get("kind") == "mutation"
        ):
            snapshot_id = data.get("snapshot_id")
            target = task["payload"].get("target") or {}
            valid_snapshot = bool(
                snapshot_id
                and isinstance(snapshot_evidence, dict)
                and snapshot_evidence.get("snapshot_id") == snapshot_id
                and snapshot_evidence.get("document_id") == target.get("document_id")
                and isinstance(snapshot_evidence.get("feature_errors"), list)
            )
            if not valid_snapshot:
                result = self._snapshot_failure(request_id, task["action_name"], result)
            else:
                snapshot = {
                    "snapshot_id": snapshot_id,
                    "owner_id": task["owner_id"],
                    "request_id": request_id,
                    "payload": snapshot_evidence,
                }

        approval = None
        if (
            task["phase"] == "preview"
            and result.get("status") == "success"
            and requires_approval(task["action_name"])
        ):
            approval = {
                "approval_id": str(uuid.uuid4()),
                "owner_id": task["owner_id"],
                "connector_instance_id": connector_id,
                "document_id": task["payload"]["target"]["document_id"],
                "intent_hash": task["intent_hash"],
                "risk": classify_risk(task["action_name"]),
                "expires_at": time.time() + 600,
            }

        completed = await asyncio.to_thread(
            self.store.complete_task,
            connector_id,
            request_id,
            str(envelope.lease_id),
            envelope.attempt,
            envelope.intent_hash,
            result,
            approval=approval,
            snapshot=snapshot,
        )
        return self.public_task(completed)

    @staticmethod
    def _verification_failure(request_id: str, action_name: str, original: dict[str, Any]) -> dict[str, Any]:
        return {
            **original,
            "request_id": request_id,
            "action": action_name,
            "status": "failed",
            "error": {
                "code": "VERIFICATION_FAILED",
                "message": "Fusion operation did not produce complete verification evidence",
                "category": "verification",
                "retryable": False,
                "details": {},
            },
        }

    @staticmethod
    def _artifact_failure(request_id: str, action_name: str, original: dict[str, Any]) -> dict[str, Any]:
        observed = original.get("verification")
        observed = observed if isinstance(observed, dict) else {}
        checks = observed.get("checks")
        checks = list(checks) if isinstance(checks, list) else []
        checks.append({"check": "artifact_valid", "passed": False, "actual": False})
        feature_errors = observed.get("new_feature_errors")
        feature_errors = list(feature_errors) if isinstance(feature_errors, list) else []
        return {
            **original,
            "request_id": request_id,
            "action": action_name,
            "status": "failed",
            "verification": {
                "passed": False,
                "checks": checks,
                "compute_completed": observed.get("compute_completed") is True,
                "new_feature_errors": feature_errors,
            },
            "error": {
                "code": "ARTIFACT_INVALID",
                "message": "Fusion export did not produce the declared verified artifact",
                "category": "artifact",
                "retryable": False,
                "details": {},
            },
        }

    @staticmethod
    def _has_complete_verification(result: dict[str, Any]) -> bool:
        verification = result.get("verification")
        if not isinstance(verification, dict):
            return False
        checks = verification.get("checks")
        new_errors = verification.get("new_feature_errors")
        return bool(
            verification.get("passed") is True
            and verification.get("compute_completed") is True
            and isinstance(new_errors, list)
            and not new_errors
            and isinstance(checks, list)
            and checks
            and all(isinstance(check, dict) and check.get("passed") is True for check in checks)
        )

    @staticmethod
    def _snapshot_failure(request_id: str, action_name: str, original: dict[str, Any]) -> dict[str, Any]:
        return {
            **original,
            "request_id": request_id,
            "action": action_name,
            "status": "failed",
            # Do not expose an identifier that was not bound to retained
            # pre-mutation evidence.
            "data": None,
            "error": {
                "code": "VERIFICATION_FAILED",
                "message": "Fusion mutation snapshot evidence is missing or inconsistent",
                "category": "verification",
                "retryable": False,
                "details": {},
            },
        }

    @staticmethod
    def public_task(task: dict[str, Any]) -> dict[str, Any]:
        if task["result"] is not None:
            return task["result"]
        status = task["status"]
        error = None
        if status in {"timeout", "indeterminate", "cancelled"}:
            codes = {"timeout": "REQUEST_TIMEOUT", "cancelled": "REQUEST_CANCELLED", "indeterminate": "VERIFICATION_FAILED"}
            categories = {"timeout": "timeout", "cancelled": "cancel", "indeterminate": "verification"}
            error = {
                "code": codes[status],
                "message": f"Fusion request ended in {status} state",
                "category": categories[status],
                "retryable": status == "timeout",
                "details": {},
            }
        return {
            "request_id": task["request_id"],
            "status": status,
            "action": task["action_name"],
            "data": None,
            "changes": [],
            "warnings": [],
            "verification": None,
            "artifacts": [],
            "approval": None,
            "error": error,
        }
