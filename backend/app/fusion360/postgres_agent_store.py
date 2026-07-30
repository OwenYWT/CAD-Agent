"""PostgreSQL/S3 store for direct Fusion Agent control-plane traffic."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db import tenant_transaction
from app.domain.identity import IDENTITY_NAMESPACE
from app.object_store import delete_object, get_object, put_file, sha256_object
from app.principal_context import current_principal

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
from .agent_store import AgentStoreError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _record_id(
    tenant_id: uuid.UUID,
    record_type: str,
    external_id: str,
) -> uuid.UUID:
    return uuid.uuid5(
        IDENTITY_NAMESPACE,
        f"fusion-agent-record:{tenant_id}:{record_type}:{external_id}",
    )


class PostgresAgentStore:
    """Privacy-bounded Agent metadata in PostgreSQL and immutable bytes in S3."""

    def __init__(
        self,
        *,
        artifact_root: str | Path,
        max_artifact_bytes: int,
    ):
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_artifact_bytes = max(1, int(max_artifact_bytes))

    def close(self) -> None:
        return None

    async def _record(
        self,
        record_type: str,
        external_id: str,
    ) -> dict[str, Any] | None:
        context = current_principal()
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT state, payload, created_at, updated_at
                        FROM connector_records
                        WHERE tenant_id=:tenant AND principal_id=:principal
                          AND connector='fusion360'
                          AND record_type=:record_type
                          AND external_id=:external_id
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "principal": context.principal_id,
                        "record_type": record_type,
                        "external_id": external_id,
                    },
                )
            ).mappings().one_or_none()
        return dict(row) if row is not None else None

    async def record_heartbeat(
        self,
        owner_id: str,
        heartbeat: AgentHeartbeatRequest,
    ) -> AgentHeartbeatReceipt:
        del owner_id
        context = current_principal()
        received_at = _now()
        external_id = str(heartbeat.connector_instance_id)
        payload = {
            "connector_instance_id": external_id,
            "fusion_version": heartbeat.fusion_version,
            "addin_version": heartbeat.addin_version,
            "platform": heartbeat.platform,
            "capabilities": heartbeat.capabilities.model_dump(mode="json"),
            "last_seen_at": received_at.isoformat(),
        }
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO connector_records (
                        id, tenant_id, principal_id, connector, record_type,
                        external_id, state, payload
                    ) VALUES (
                        :id, :tenant, :principal, 'fusion360',
                        'agent_connector', :external_id, 'online',
                        CAST(:payload AS jsonb)
                    )
                    ON CONFLICT (
                        tenant_id, connector, record_type, external_id
                    ) DO UPDATE SET
                        principal_id=EXCLUDED.principal_id,
                        state='online',
                        payload=EXCLUDED.payload,
                        updated_at=CURRENT_TIMESTAMP
                    """
                ),
                {
                    "id": _record_id(
                        context.tenant_id,
                        "agent_connector",
                        external_id,
                    ),
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                    "external_id": external_id,
                    "payload": json.dumps(payload, separators=(",", ":")),
                },
            )
        return AgentHeartbeatReceipt(
            connector_instance_id=heartbeat.connector_instance_id,
            received_at=received_at,
        )

    async def connector_status(
        self,
        owner_id: str,
        connector_instance_id: uuid.UUID | str,
        *,
        ttl_s: int = 15,
    ) -> AgentConnectorStatus:
        del owner_id
        connector_id = str(connector_instance_id)
        row = await self._record("agent_connector", connector_id)
        if row is None:
            return AgentConnectorStatus(
                connector_instance_id=uuid.UUID(connector_id),
                connector_online=False,
                fusion_running=None,
            )
        payload = dict(row["payload"])
        last_seen_at = datetime.fromisoformat(str(payload["last_seen_at"]))
        online = (_now() - last_seen_at).total_seconds() <= max(1, ttl_s)
        from .contract import CadCapabilities

        capabilities = CadCapabilities.model_validate(payload["capabilities"])
        capabilities = capabilities.model_copy(
            update={
                "available": online,
                "connector_online": online,
                "fusion_running": True if online else None,
                "last_heartbeat_at": last_seen_at,
            }
        )
        return AgentConnectorStatus(
            connector_instance_id=uuid.UUID(connector_id),
            connector_online=online,
            fusion_running=True if online else None,
            last_heartbeat_at=last_seen_at,
            fusion_version=payload["fusion_version"],
            addin_version=payload["addin_version"],
            platform=payload["platform"],
            capabilities=capabilities,
        )

    async def save_plan(
        self,
        *,
        owner_id: str,
        turn_hash: str,
        response: AgentPlanResponse,
        action_intent_hash: str | None,
        export_upload_consent: bool,
        f3d_upload_authorized: bool,
    ) -> tuple[AgentPlanResponse, bool]:
        del owner_id
        context = current_principal()
        request_id = str(response.request_id)
        export_format = None
        export_filename = None
        if response.action and response.action.action == "cad.export":
            export_format = response.action.format
            export_filename = response.action.filename
        payload = {
            "request_id": request_id,
            "turn_hash": turn_hash,
            "proposal_id": str(response.proposal_id),
            "connector_instance_id": str(response.connector_instance_id),
            "context_fingerprint": response.context_fingerprint,
            "status": response.status,
            "action_name": response.action.action if response.action else None,
            "action_intent_hash": action_intent_hash,
            "action": (
                response.action.model_dump(mode="json")
                if response.action
                else None
            ),
            "response": response.model_dump(mode="json"),
            "export_format": export_format,
            "export_filename": export_filename,
            "export_upload_consent": export_upload_consent,
            "f3d_upload_authorized": f3d_upload_authorized,
        }
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            existing = (
                await connection.execute(
                    text(
                        """
                        SELECT payload FROM connector_records
                        WHERE tenant_id=:tenant AND principal_id=:principal
                          AND connector='fusion360'
                          AND record_type='agent_plan'
                          AND external_id=:request
                        FOR UPDATE
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "principal": context.principal_id,
                        "request": request_id,
                    },
                )
            ).mappings().one_or_none()
            if existing is not None:
                stored = dict(existing["payload"])
                if stored["turn_hash"] != turn_hash:
                    raise AgentStoreError(
                        "request_id was reused with a different Agent turn",
                        code="AGENT_IDEMPOTENCY_CONFLICT",
                        http_status=409,
                    )
                return (
                    AgentPlanResponse.model_validate(stored["response"]),
                    True,
                )
            await connection.execute(
                text(
                    """
                    INSERT INTO connector_records (
                        id, tenant_id, principal_id, connector, record_type,
                        external_id, state, payload
                    ) VALUES (
                        :id, :tenant, :principal, 'fusion360',
                        'agent_plan', :request, :state,
                        CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "id": _record_id(
                        context.tenant_id,
                        "agent_plan",
                        request_id,
                    ),
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                    "request": request_id,
                    "state": response.status,
                    "payload": json.dumps(payload, separators=(",", ":")),
                },
            )
        return response, False

    async def plan_record(
        self,
        owner_id: str,
        request_id: uuid.UUID | str,
    ) -> dict[str, Any]:
        del owner_id
        row = await self._record("agent_plan", str(request_id))
        if row is None:
            raise AgentStoreError(
                "Agent plan was not found",
                code="AGENT_PLAN_NOT_FOUND",
                http_status=404,
            )
        return dict(row["payload"])

    async def find_plan(
        self,
        owner_id: str,
        request_id: uuid.UUID | str,
        turn_hash: str,
    ) -> AgentPlanResponse | None:
        del owner_id
        row = await self._record("agent_plan", str(request_id))
        if row is None:
            return None
        payload = dict(row["payload"])
        if payload["turn_hash"] != turn_hash:
            raise AgentStoreError(
                "request_id was reused with a different Agent turn",
                code="AGENT_IDEMPOTENCY_CONFLICT",
                http_status=409,
            )
        return AgentPlanResponse.model_validate(payload["response"])

    async def save_report(
        self,
        *,
        owner_id: str,
        report_hash: str,
        report: AgentExecutionReport,
    ) -> AgentReportReceipt:
        del owner_id
        context = current_principal()
        report_id = str(report.report_id)
        received_at = _now()
        verification_passed = (
            None
            if report.result.verification is None
            else report.result.verification.passed
        )
        payload = {
            "report_id": report_id,
            "request_id": str(report.request_id),
            "proposal_id": str(report.proposal_id),
            "report_hash": report_hash,
            "connector_instance_id": str(report.connector_instance_id),
            "context_fingerprint": report.context_fingerprint,
            "action_intent_hash": report.action_intent_hash,
            "result_status": report.result.status,
            "result_action": report.result.action,
            "verification_passed": verification_passed,
            "artifact_count": len(report.result.artifacts),
            "completed_at": report.completed_at.isoformat(),
            "received_at": received_at.isoformat(),
        }
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            existing = (
                await connection.execute(
                    text(
                        """
                        SELECT payload FROM connector_records
                        WHERE tenant_id=:tenant AND principal_id=:principal
                          AND connector='fusion360'
                          AND record_type='agent_report'
                          AND external_id=:report
                        FOR UPDATE
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "principal": context.principal_id,
                        "report": report_id,
                    },
                )
            ).mappings().one_or_none()
            if existing is not None:
                stored = dict(existing["payload"])
                if stored["report_hash"] != report_hash:
                    raise AgentStoreError(
                        "report_id was reused with different result data",
                        code="AGENT_IDEMPOTENCY_CONFLICT",
                        http_status=409,
                    )
                return AgentReportReceipt(
                    request_id=report.request_id,
                    report_id=report.report_id,
                    status="duplicate",
                    received_at=datetime.fromisoformat(stored["received_at"]),
                )
            await connection.execute(
                text(
                    """
                    INSERT INTO connector_records (
                        id, tenant_id, principal_id, connector, record_type,
                        external_id, state, payload
                    ) VALUES (
                        :id, :tenant, :principal, 'fusion360',
                        'agent_report', :report, :state,
                        CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "id": _record_id(
                        context.tenant_id,
                        "agent_report",
                        report_id,
                    ),
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                    "report": report_id,
                    "state": report.result.status,
                    "payload": json.dumps(payload, separators=(",", ":")),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO connector_audit_records (
                        id, tenant_id, principal_id, connector, request_id,
                        event_type, payload
                    ) VALUES (
                        :id, :tenant, :principal, 'fusion360', :request,
                        'agent_execution_report', CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "id": uuid.uuid5(
                        IDENTITY_NAMESPACE,
                        f"fusion-agent-audit:{context.tenant_id}:{report_id}",
                    ),
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                    "request": str(report.request_id),
                    "payload": json.dumps(payload, separators=(",", ":")),
                },
            )
        return AgentReportReceipt(
            request_id=report.request_id,
            report_id=report.report_id,
            status="accepted",
            received_at=received_at,
        )

    async def has_successful_export_report(
        self,
        owner_id: str,
        request_id: uuid.UUID | str,
    ) -> bool:
        del owner_id
        context = current_principal()
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT payload FROM connector_records
                        WHERE tenant_id=:tenant AND principal_id=:principal
                          AND connector='fusion360'
                          AND record_type='agent_report'
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "principal": context.principal_id,
                    },
                )
            ).scalars().all()
        request_name = str(request_id)
        return any(
            record.get("request_id") == request_name
            and record.get("result_status") == "success"
            and record.get("result_action") == "cad.export"
            for record in rows
        )

    async def artifact_record(
        self,
        owner_id: str,
        request_id: uuid.UUID | str,
        filename: str,
    ) -> dict[str, Any]:
        del owner_id
        context = current_principal()
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT request_id, filename, size_bytes, sha256,
                               content_type AS media_type, object_key, created_at
                        FROM connector_artifacts
                        WHERE tenant_id=:tenant AND principal_id=:principal
                          AND connector='fusion360' AND request_id=:request
                          AND filename=:filename
                        """
                    ),
                    {
                        "tenant": context.tenant_id,
                        "principal": context.principal_id,
                        "request": str(request_id),
                        "filename": filename,
                    },
                )
            ).mappings().one_or_none()
        if row is None:
            raise AgentStoreError(
                "Agent artifact was not found",
                code="AGENT_ARTIFACT_NOT_FOUND",
                http_status=404,
            )
        return dict(row)

    async def artifact_bytes(
        self,
        owner_id: str,
        request_id: uuid.UUID | str,
        filename: str,
    ) -> tuple[dict[str, Any], bytes]:
        record = await self.artifact_record(owner_id, request_id, filename)
        payload = await get_object(record["object_key"])
        if (
            len(payload) != int(record["size_bytes"])
            or hashlib.sha256(payload).hexdigest() != record["sha256"]
        ):
            raise AgentStoreError(
                "Agent artifact checksum verification failed",
                code="AGENT_ARTIFACT_NOT_FOUND",
                http_status=404,
            )
        return record, payload

    async def authorize_artifact(
        self,
        owner_id: str,
        claim: AgentArtifactUploadClaim,
    ) -> dict[str, Any]:
        record = await self.plan_record(owner_id, claim.request_id)
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
        if not await self.has_successful_export_report(
            owner_id,
            claim.request_id,
        ):
            raise AgentStoreError(
                "artifact upload requires a successful execution report for the export",
                code="AGENT_ARTIFACT_NOT_AUTHORIZED",
                http_status=403,
            )
        return record

    def staging_path(
        self,
        owner_id: str,
        request_id: uuid.UUID | str,
        filename: str,
    ) -> Path:
        request_name = str(uuid.UUID(str(request_id)))
        owner_hash = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:24]
        request_dir = (self.artifact_root / owner_hash / request_name).resolve()
        if not request_dir.is_relative_to(self.artifact_root):
            raise AgentStoreError(
                "artifact path escaped its request",
                code="AGENT_ARTIFACT_INVALID",
                http_status=422,
            )
        request_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = (request_dir / filename).resolve()
        if path.parent != request_dir:
            raise AgentStoreError(
                "artifact path escaped its request",
                code="AGENT_ARTIFACT_INVALID",
                http_status=422,
            )
        return path.with_name(f".{path.name}.{uuid.uuid4().hex}.upload")

    async def commit_artifact(
        self,
        *,
        owner_id: str,
        claim: AgentArtifactUploadClaim,
        media_type: str,
        temporary_path: Path,
    ) -> AgentArtifactReceipt:
        del owner_id
        context = current_principal()
        existing = await self._artifact_if_exists(claim)
        if existing is not None:
            temporary_path.unlink(missing_ok=True)
            return self._duplicate_or_conflict(existing, claim)

        object_key = (
            f"connectors/tenants/{context.tenant_id}/fusion360/"
            f"{claim.request_id}/{claim.sha256}/{claim.filename}"
        )
        uploaded = await put_file(
            object_key,
            temporary_path,
            content_type=media_type,
        )
        temporary_path.unlink(missing_ok=True)
        if (
            uploaded["size_bytes"] != claim.size_bytes
            or uploaded["sha256"] != claim.sha256
        ):
            raise AgentStoreError(
                "artifact upload verification failed",
                code="AGENT_ARTIFACT_INVALID",
                http_status=422,
            )

        inserted = False
        try:
            async with tenant_transaction(
                context.tenant_id,
                context.principal_id,
            ) as connection:
                inserted = bool(
                    (
                        await connection.execute(
                            text(
                                """
                                INSERT INTO connector_artifacts (
                                    id, tenant_id, principal_id, connector,
                                    request_id, filename, content_type,
                                    size_bytes, sha256, object_key
                                ) VALUES (
                                    :id, :tenant, :principal, 'fusion360',
                                    :request, :filename, :content_type,
                                    :size, :sha256, :object_key
                                )
                                ON CONFLICT (
                                    tenant_id, connector, request_id, filename
                                ) DO NOTHING
                                RETURNING id
                                """
                            ),
                            {
                                "id": uuid.uuid5(
                                    IDENTITY_NAMESPACE,
                                    "fusion-agent-artifact:"
                                    f"{context.tenant_id}:{claim.request_id}:"
                                    f"{claim.filename}:{claim.sha256}",
                                ),
                                "tenant": context.tenant_id,
                                "principal": context.principal_id,
                                "request": str(claim.request_id),
                                "filename": claim.filename,
                                "content_type": media_type,
                                "size": claim.size_bytes,
                                "sha256": claim.sha256,
                                "object_key": object_key,
                            },
                        )
                    ).scalar_one_or_none()
                )
            if not inserted:
                existing = await self._artifact_if_exists(claim)
                if existing is None:
                    raise RuntimeError("artifact metadata insert was lost")
                receipt = self._duplicate_or_conflict(existing, claim)
                if existing["object_key"] != object_key:
                    await delete_object(object_key)
                return receipt
        except Exception:
            if not inserted:
                await delete_object(object_key)
            raise
        return AgentArtifactReceipt(
            request_id=claim.request_id,
            filename=claim.filename,
            size_bytes=claim.size_bytes,
            sha256=claim.sha256,
            status="accepted",
        )

    async def _artifact_if_exists(
        self,
        claim: AgentArtifactUploadClaim,
    ) -> dict[str, Any] | None:
        try:
            return await self.artifact_record(
                "",
                claim.request_id,
                claim.filename,
            )
        except AgentStoreError as exc:
            if exc.code == "AGENT_ARTIFACT_NOT_FOUND":
                return None
            raise

    @staticmethod
    def _duplicate_or_conflict(
        existing: dict[str, Any],
        claim: AgentArtifactUploadClaim,
    ) -> AgentArtifactReceipt:
        if (
            int(existing["size_bytes"]) != claim.size_bytes
            or existing["sha256"] != claim.sha256
        ):
            raise AgentStoreError(
                "artifact filename was reused with different content",
                code="AGENT_IDEMPOTENCY_CONFLICT",
                http_status=409,
            )
        return AgentArtifactReceipt(
            request_id=claim.request_id,
            filename=claim.filename,
            size_bytes=claim.size_bytes,
            sha256=claim.sha256,
            status="duplicate",
        )

    async def verify_artifact_object(
        self,
        request_id: uuid.UUID | str,
        filename: str,
    ) -> dict[str, Any]:
        record = await self.artifact_record("", request_id, filename)
        stored = await sha256_object(record["object_key"])
        if (
            stored["size_bytes"] != int(record["size_bytes"])
            or stored["sha256"] != record["sha256"]
        ):
            raise AgentStoreError(
                "Agent artifact checksum verification failed",
                code="AGENT_ARTIFACT_NOT_FOUND",
                http_status=404,
            )
        return record
