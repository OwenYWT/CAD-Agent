"""Real PostgreSQL/MinIO coverage for Fusion control-plane stores."""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import text

from app.config import settings
from app.db import close_database, tenant_transaction
from app.domain.identity import user_principal
from app.fusion360.agent_contract import (
    AgentArtifactUploadClaim,
    AgentExecutionReport,
    AgentHeartbeatRequest,
    AgentPlanResponse,
)
from app.fusion360.agent_store import AgentStoreError
from app.fusion360.cloud import ApsClient, ApsConfig
from app.fusion360.contract import CAD_ACTION_ADAPTER
from app.fusion360.errors import FusionConnectorError
from app.fusion360.policy import action_intent_hash, sha256_canonical
from app.fusion360.postgres_agent_store import PostgresAgentStore
from app.fusion360.postgres_token_store import PostgresEncryptedTokenStore
from app.principal_context import bind_principal
from app.repositories.identity import reconcile_principal


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not settings.object_store_endpoint_url,
    reason="real PostgreSQL and S3-compatible storage are required",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    if not TEST_DATABASE_URL:
        yield
        return
    original = (
        settings.database_url,
        settings.durable_control_plane_enabled,
    )
    settings.database_url = TEST_DATABASE_URL
    settings.durable_control_plane_enabled = True
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield
    (
        settings.database_url,
        settings.durable_control_plane_enabled,
    ) = original


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def database_engine_lifecycle(migrated_database):
    yield
    await close_database()


@pytest.mark.asyncio(loop_scope="module")
async def test_fusion_oauth_tokens_are_encrypted_one_time_and_tenant_isolated():
    suffix = uuid.uuid4().hex
    owner = f"user:oauth-{suffix}"
    context = await reconcile_principal(user_principal(f"oauth-{suffix}"))
    bind_principal(context)
    store = PostgresEncryptedTokenStore(Fernet.generate_key())
    client = ApsClient(
        ApsConfig(
            enabled=True,
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="http://localhost/callback",
        ),
        store,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "access_token": f"access-{suffix}",
                    "refresh_token": f"refresh-{suffix}",
                    "expires_in": 3600,
                    "scope": "data:read",
                },
            )
        ),
    )

    started = await client.start_oauth_async(owner)
    assert await client.exchange_code(started["state"], "code") == owner
    token = await store.get(owner)
    assert token and token["access_token"] == f"access-{suffix}"
    encrypted = await store.encrypted_bytes(owner)
    assert encrypted and f"access-{suffix}".encode() not in encrypted
    with pytest.raises(FusionConnectorError):
        await store.consume_state(started["state"])

    intruder = await reconcile_principal(
        user_principal(f"oauth-intruder-{suffix}")
    )
    bind_principal(intruder)
    assert await store.get("anything") is None
    await store.delete("anything")


@pytest.mark.asyncio(loop_scope="module")
async def test_fusion_agent_metadata_audit_and_artifacts_are_durable(tmp_path):
    suffix = uuid.uuid4().hex
    context = await reconcile_principal(user_principal(f"agent-{suffix}"))
    bind_principal(context)
    store = PostgresAgentStore(
        artifact_root=tmp_path / "staging",
        max_artifact_bytes=1024 * 1024,
    )
    connector_id = uuid.uuid4()
    heartbeat = AgentHeartbeatRequest.model_validate(
        {
            "contract_version": "1.0.0",
            "connector_instance_id": str(connector_id),
            "fusion_version": "2.x",
            "addin_version": "1.0.0",
            "platform": "macos",
            "capabilities": {
                "adapter": "fusion360",
                "available": True,
                "connector_online": True,
                "fusion_running": True,
                "actions": ["cad.export"],
                "context_sections": ["document"],
            },
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    receipt = await store.record_heartbeat("ignored", heartbeat)
    assert receipt.status == "online"
    assert (
        await store.connector_status("ignored", connector_id)
    ).connector_online

    request_id = uuid.uuid4()
    action = CAD_ACTION_ADAPTER.validate_python(
        {
            "request_id": str(request_id),
            "connector_instance_id": str(connector_id),
            "action": "cad.export",
            "target": {"document_id": "doc-1"},
            "format": "step",
            "filename": "part.step",
        }
    )
    response = AgentPlanResponse(
        request_id=request_id,
        proposal_id=uuid.uuid4(),
        connector_instance_id=connector_id,
        status="proposed",
        context_fingerprint="ctx-c14n-1:" + "a" * 64,
        action=action,
        risk="low",
        reason="Export the approved part",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    first, duplicate = await store.save_plan(
        owner_id="ignored",
        turn_hash="b" * 64,
        response=response,
        action_intent_hash=action_intent_hash(action),
        export_upload_consent=True,
        f3d_upload_authorized=False,
    )
    replay, replayed = await store.save_plan(
        owner_id="ignored",
        turn_hash="b" * 64,
        response=response,
        action_intent_hash=action_intent_hash(action),
        export_upload_consent=True,
        f3d_upload_authorized=False,
    )
    assert first == replay and duplicate is False and replayed is True

    report = AgentExecutionReport.model_validate(
        {
            "contract_version": "1.0.0",
            "report_id": str(uuid.uuid4()),
            "request_id": str(request_id),
            "proposal_id": str(response.proposal_id),
            "connector_instance_id": str(connector_id),
            "context_fingerprint": response.context_fingerprint,
            "action_intent_hash": action_intent_hash(action),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "result": {
                "request_id": str(request_id),
                "status": "success",
                "action": "cad.export",
                "data": {
                    "kind": "export",
                    "format": "step",
                    "artifact_ids": ["artifact-1"],
                },
                "verification": {
                    "passed": True,
                    "checks": [{"check": "artifact_valid", "passed": True}],
                    "compute_completed": True,
                    "new_feature_errors": [],
                },
            },
        }
    )
    report_hash = sha256_canonical(report.model_dump(mode="json"))
    accepted = await store.save_report(
        owner_id="ignored",
        report_hash=report_hash,
        report=report,
    )
    replayed_report = await store.save_report(
        owner_id="ignored",
        report_hash=report_hash,
        report=report,
    )
    assert accepted.status == "accepted"
    assert replayed_report.status == "duplicate"

    payload = b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;"
    claim = AgentArtifactUploadClaim(
        request_id=request_id,
        filename="part.step",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    await store.authorize_artifact("ignored", claim)
    temporary = store.staging_path("owner", request_id, claim.filename)
    temporary.write_bytes(payload)
    committed = await store.commit_artifact(
        owner_id="ignored",
        claim=claim,
        media_type="application/step",
        temporary_path=temporary,
    )
    assert committed.status == "accepted"
    record, downloaded = await store.artifact_bytes(
        "ignored",
        request_id,
        claim.filename,
    )
    assert downloaded == payload
    assert record["sha256"] == claim.sha256

    second_temporary = store.staging_path(
        "owner",
        request_id,
        claim.filename,
    )
    second_temporary.write_bytes(payload)
    second = await store.commit_artifact(
        owner_id="ignored",
        claim=claim,
        media_type="application/step",
        temporary_path=second_temporary,
    )
    assert second.status == "duplicate"

    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        audits = (
            await connection.execute(
                text(
                    """
                    SELECT payload FROM connector_audit_records
                    WHERE tenant_id=:tenant AND principal_id=:principal
                      AND connector='fusion360' AND request_id=:request
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                    "request": str(request_id),
                },
            )
        ).scalars().all()
    assert len(audits) == 1
    assert "Export the approved part" not in str(audits)
    assert str(tmp_path) not in str(audits)

    intruder = await reconcile_principal(
        user_principal(f"agent-intruder-{suffix}")
    )
    bind_principal(intruder)
    assert await store.find_plan("ignored", request_id, "b" * 64) is None
    with pytest.raises(AgentStoreError):
        await store.artifact_record("ignored", request_id, claim.filename)
