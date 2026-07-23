"""Durable, privacy-bounded audit store for direct Fusion Agent traffic."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .agent_contract import (
    AgentArtifactReceipt,
    AgentArtifactUploadClaim,
    AgentConnectorStatus,
    AgentExecutionReport,
    AgentHeartbeatReceipt,
    AgentHeartbeatRequest,
    AgentPlanResponse,
    AgentReportReceipt,
)


class AgentStoreError(Exception):
    def __init__(self, message: str, *, code: str, http_status: int):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


class AgentStore:
    """SQLite plan/result metadata plus request-confined artifact files.

    Prompt text, Fusion context, bearer credentials, complete execution results and
    full CAD payloads are not written to SQLite.  Exact plan actions are retained
    because report and upload authorization must be checked after process restart.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        artifact_root: str | Path,
        max_artifact_bytes: int,
    ):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_artifact_bytes = max(1, int(max_artifact_bytes))
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
                CREATE TABLE IF NOT EXISTS agent_plans (
                    owner_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    turn_hash TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    connector_instance_id TEXT NOT NULL,
                    context_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    action_name TEXT,
                    action_intent_hash TEXT,
                    action_json TEXT,
                    response_json TEXT NOT NULL,
                    export_format TEXT,
                    export_filename TEXT,
                    export_upload_consent INTEGER NOT NULL,
                    f3d_upload_authorized INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(owner_id, request_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_plan_proposal
                    ON agent_plans(owner_id, proposal_id);
                CREATE TABLE IF NOT EXISTS agent_reports (
                    owner_id TEXT NOT NULL,
                    report_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    report_hash TEXT NOT NULL,
                    connector_instance_id TEXT NOT NULL,
                    context_fingerprint TEXT NOT NULL,
                    action_intent_hash TEXT NOT NULL,
                    result_status TEXT NOT NULL,
                    result_action TEXT NOT NULL,
                    verification_passed INTEGER,
                    artifact_count INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    PRIMARY KEY(owner_id, report_id),
                    FOREIGN KEY(owner_id, request_id) REFERENCES agent_plans(owner_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_reports_request
                    ON agent_reports(owner_id, request_id, received_at);
                CREATE TABLE IF NOT EXISTS agent_artifacts (
                    owner_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(owner_id, request_id, filename),
                    FOREIGN KEY(owner_id, request_id) REFERENCES agent_plans(owner_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS agent_connectors (
                    owner_id TEXT NOT NULL,
                    connector_instance_id TEXT NOT NULL,
                    fusion_version TEXT NOT NULL,
                    addin_version TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL,
                    last_seen_at REAL NOT NULL,
                    PRIMARY KEY(owner_id, connector_instance_id)
                );
                """
            )

    def record_heartbeat(
        self, owner_id: str, heartbeat: AgentHeartbeatRequest
    ) -> AgentHeartbeatReceipt:
        now = time.time()
        capabilities_json = _dump(heartbeat.capabilities.model_dump(mode="json"))
        with self._lock:
            self._db.execute(
                """INSERT INTO agent_connectors (
                    owner_id,connector_instance_id,fusion_version,addin_version,
                    platform,capabilities_json,last_seen_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(owner_id,connector_instance_id) DO UPDATE SET
                    fusion_version=excluded.fusion_version,
                    addin_version=excluded.addin_version,
                    platform=excluded.platform,
                    capabilities_json=excluded.capabilities_json,
                    last_seen_at=excluded.last_seen_at""",
                (
                    owner_id,
                    str(heartbeat.connector_instance_id),
                    heartbeat.fusion_version,
                    heartbeat.addin_version,
                    heartbeat.platform,
                    capabilities_json,
                    now,
                ),
            )
        return AgentHeartbeatReceipt(
            connector_instance_id=heartbeat.connector_instance_id,
            received_at=datetime_from_epoch(now),
        )

    def connector_status(
        self, owner_id: str, connector_instance_id: uuid.UUID | str, *, ttl_s: int = 15
    ) -> AgentConnectorStatus:
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM agent_connectors
                WHERE owner_id=? AND connector_instance_id=?""",
                (owner_id, str(connector_instance_id)),
            ).fetchone()
        if row is None:
            return AgentConnectorStatus(
                connector_instance_id=uuid.UUID(str(connector_instance_id)),
                connector_online=False,
                fusion_running=None,
            )
        online = time.time() - float(row["last_seen_at"]) <= max(1, ttl_s)
        from .contract import CadCapabilities

        capabilities = CadCapabilities.model_validate_json(row["capabilities_json"])
        capabilities = capabilities.model_copy(update={
            "available": online,
            "connector_online": online,
            "fusion_running": True if online else None,
            "last_heartbeat_at": datetime_from_epoch(row["last_seen_at"]),
        })
        return AgentConnectorStatus(
            connector_instance_id=uuid.UUID(str(connector_instance_id)),
            connector_online=online,
            fusion_running=True if online else None,
            last_heartbeat_at=datetime_from_epoch(row["last_seen_at"]),
            fusion_version=row["fusion_version"],
            addin_version=row["addin_version"],
            platform=row["platform"],
            capabilities=capabilities,
        )

    def save_plan(
        self,
        *,
        owner_id: str,
        turn_hash: str,
        response: AgentPlanResponse,
        action_intent_hash: str | None,
        export_upload_consent: bool,
        f3d_upload_authorized: bool,
    ) -> tuple[AgentPlanResponse, bool]:
        request_id = str(response.request_id)
        response_json = _dump(response.model_dump(mode="json"))
        action_json = _dump(response.action.model_dump(mode="json")) if response.action else None
        export_format = None
        export_filename = None
        if response.action and response.action.action == "cad.export":
            export_format = response.action.format
            export_filename = response.action.filename
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._db.execute(
                    "SELECT turn_hash,response_json FROM agent_plans WHERE owner_id=? AND request_id=?",
                    (owner_id, request_id),
                ).fetchone()
                if existing:
                    if existing["turn_hash"] != turn_hash:
                        raise AgentStoreError(
                            "request_id was reused with a different Agent turn",
                            code="AGENT_IDEMPOTENCY_CONFLICT",
                            http_status=409,
                        )
                    self._db.execute("COMMIT")
                    return AgentPlanResponse.model_validate_json(existing["response_json"]), True
                self._db.execute(
                    """INSERT INTO agent_plans (
                        owner_id,request_id,turn_hash,proposal_id,connector_instance_id,
                        context_fingerprint,status,action_name,action_intent_hash,action_json,
                        response_json,export_format,export_filename,export_upload_consent,
                        f3d_upload_authorized,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        owner_id,
                        request_id,
                        turn_hash,
                        str(response.proposal_id),
                        str(response.connector_instance_id),
                        response.context_fingerprint,
                        response.status,
                        response.action.action if response.action else None,
                        action_intent_hash,
                        action_json,
                        response_json,
                        export_format,
                        export_filename,
                        int(export_upload_consent),
                        int(f3d_upload_authorized),
                        now,
                    ),
                )
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        return response, False

    def plan_record(self, owner_id: str, request_id: uuid.UUID | str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM agent_plans WHERE owner_id=? AND request_id=?",
                (owner_id, str(request_id)),
            ).fetchone()
        if row is None:
            raise AgentStoreError("Agent plan was not found", code="AGENT_PLAN_NOT_FOUND", http_status=404)
        record = dict(row)
        record["action"] = json.loads(record["action_json"]) if record["action_json"] else None
        return record

    def find_plan(
        self, owner_id: str, request_id: uuid.UUID | str, turn_hash: str
    ) -> AgentPlanResponse | None:
        with self._lock:
            row = self._db.execute(
                "SELECT turn_hash,response_json FROM agent_plans WHERE owner_id=? AND request_id=?",
                (owner_id, str(request_id)),
            ).fetchone()
        if row is None:
            return None
        if row["turn_hash"] != turn_hash:
            raise AgentStoreError(
                "request_id was reused with a different Agent turn",
                code="AGENT_IDEMPOTENCY_CONFLICT",
                http_status=409,
            )
        return AgentPlanResponse.model_validate_json(row["response_json"])

    def save_report(
        self,
        *,
        owner_id: str,
        report_hash: str,
        report: AgentExecutionReport,
    ) -> AgentReportReceipt:
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._db.execute(
                    "SELECT * FROM agent_reports WHERE owner_id=? AND report_id=?",
                    (owner_id, str(report.report_id)),
                ).fetchone()
                if existing:
                    if existing["report_hash"] != report_hash:
                        raise AgentStoreError(
                            "report_id was reused with different result data",
                            code="AGENT_IDEMPOTENCY_CONFLICT",
                            http_status=409,
                        )
                    self._db.execute("COMMIT")
                    return AgentReportReceipt(
                        request_id=report.request_id,
                        report_id=report.report_id,
                        status="duplicate",
                        received_at=datetime_from_epoch(existing["received_at"]),
                    )
                self._db.execute(
                    """INSERT INTO agent_reports (
                        owner_id,report_id,request_id,proposal_id,report_hash,
                        connector_instance_id,context_fingerprint,action_intent_hash,
                        result_status,result_action,verification_passed,artifact_count,
                        completed_at,received_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        owner_id,
                        str(report.report_id),
                        str(report.request_id),
                        str(report.proposal_id),
                        report_hash,
                        str(report.connector_instance_id),
                        report.context_fingerprint,
                        report.action_intent_hash,
                        report.result.status,
                        report.result.action,
                        None if report.result.verification is None else int(report.result.verification.passed),
                        len(report.result.artifacts),
                        report.completed_at.isoformat(),
                        now,
                    ),
                )
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        return AgentReportReceipt(
            request_id=report.request_id,
            report_id=report.report_id,
            status="accepted",
            received_at=datetime_from_epoch(now),
        )

    def has_successful_export_report(self, owner_id: str, request_id: uuid.UUID | str) -> bool:
        with self._lock:
            row = self._db.execute(
                """SELECT 1 FROM agent_reports WHERE owner_id=? AND request_id=?
                AND result_status='success' AND result_action='cad.export' LIMIT 1""",
                (owner_id, str(request_id)),
            ).fetchone()
        return row is not None

    def artifact_record(
        self, owner_id: str, request_id: uuid.UUID | str, filename: str
    ) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM agent_artifacts
                WHERE owner_id=? AND request_id=? AND filename=?""",
                (owner_id, str(request_id), filename),
            ).fetchone()
        if row is None:
            raise AgentStoreError(
                "Agent artifact was not found",
                code="AGENT_ARTIFACT_NOT_FOUND",
                http_status=404,
            )
        record = dict(row)
        request_name = str(uuid.UUID(str(request_id)))
        owner_hash = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:24]
        stored_path = (self.artifact_root / owner_hash / request_name / filename).resolve()
        if not stored_path.is_relative_to(self.artifact_root) or not stored_path.is_file():
            raise AgentStoreError(
                "Agent artifact is unavailable",
                code="AGENT_ARTIFACT_NOT_FOUND",
                http_status=404,
            )
        record["stored_path"] = stored_path
        return record

    def authorize_artifact(self, owner_id: str, claim: AgentArtifactUploadClaim) -> dict[str, Any]:
        record = self.plan_record(owner_id, claim.request_id)
        if record["status"] != "proposed" or record["action_name"] != "cad.export":
            raise AgentStoreError(
                "artifact upload requires an explicit proposed export action",
                code="AGENT_ARTIFACT_NOT_AUTHORIZED",
                http_status=403,
            )
        if not record["export_upload_consent"]:
            raise AgentStoreError(
                "artifact upload was not explicitly authorized by the user",
                code="AGENT_ARTIFACT_NOT_AUTHORIZED",
                http_status=403,
            )
        if record["export_filename"] != claim.filename:
            raise AgentStoreError(
                "artifact filename is outside the request-scoped export action",
                code="AGENT_ARTIFACT_NOT_AUTHORIZED",
                http_status=403,
            )
        if record["export_format"] == "f3d" and not record["f3d_upload_authorized"]:
            raise AgentStoreError(
                "full F3D upload requires separate explicit authorization",
                code="AGENT_F3D_AUTHORIZATION_REQUIRED",
                http_status=403,
            )
        if not self.has_successful_export_report(owner_id, claim.request_id):
            raise AgentStoreError(
                "artifact upload requires a successful execution report for the export",
                code="AGENT_ARTIFACT_NOT_AUTHORIZED",
                http_status=403,
            )
        return record

    def staging_path(self, owner_id: str, request_id: uuid.UUID | str, filename: str) -> Path:
        request_name = str(uuid.UUID(str(request_id)))
        owner_hash = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:24]
        request_dir = (self.artifact_root / owner_hash / request_name).resolve()
        if not request_dir.is_relative_to(self.artifact_root):
            raise AgentStoreError("artifact path escaped its request", code="AGENT_ARTIFACT_INVALID", http_status=422)
        request_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = (request_dir / filename).resolve()
        if path.parent != request_dir:
            raise AgentStoreError("artifact path escaped its request", code="AGENT_ARTIFACT_INVALID", http_status=422)
        return path.with_name(f".{path.name}.{uuid.uuid4().hex}.upload")

    def commit_artifact(
        self,
        *,
        owner_id: str,
        claim: AgentArtifactUploadClaim,
        media_type: str,
        temporary_path: Path,
    ) -> AgentArtifactReceipt:
        final_path = temporary_path.with_name(claim.filename)
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._db.execute(
                    """SELECT * FROM agent_artifacts
                    WHERE owner_id=? AND request_id=? AND filename=?""",
                    (owner_id, str(claim.request_id), claim.filename),
                ).fetchone()
                if existing:
                    if existing["size_bytes"] != claim.size_bytes or existing["sha256"] != claim.sha256:
                        raise AgentStoreError(
                            "artifact filename was reused with different content",
                            code="AGENT_IDEMPOTENCY_CONFLICT",
                            http_status=409,
                        )
                    self._db.execute("COMMIT")
                    temporary_path.unlink(missing_ok=True)
                    return AgentArtifactReceipt(
                        request_id=claim.request_id,
                        filename=claim.filename,
                        size_bytes=claim.size_bytes,
                        sha256=claim.sha256,
                        status="duplicate",
                    )
                os.replace(temporary_path, final_path)
                self._db.execute(
                    """INSERT INTO agent_artifacts (
                        owner_id,request_id,filename,size_bytes,sha256,media_type,created_at
                    ) VALUES (?,?,?,?,?,?,?)""",
                    (
                        owner_id,
                        str(claim.request_id),
                        claim.filename,
                        claim.size_bytes,
                        claim.sha256,
                        media_type,
                        now,
                    ),
                )
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        return AgentArtifactReceipt(
            request_id=claim.request_id,
            filename=claim.filename,
            size_bytes=claim.size_bytes,
            sha256=claim.sha256,
            status="accepted",
        )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def datetime_from_epoch(value: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value, timezone.utc)
