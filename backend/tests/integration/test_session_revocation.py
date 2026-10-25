"""Real PostgreSQL auth and production WebSocket routes, without model calls."""
import asyncio
import base64
from contextlib import asynccontextmanager
import os
from pathlib import Path
import secrets
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from app.api.login import router as login_router
from app.api.tasks import durable_task_websocket, router as tasks_router
from app.api.websocket import websocket_endpoint
from app.config import settings
from app.db import close_database, tenant_transaction
from app.domain.identity import user_principal
from app.repositories.projects import create_project
from app.repositories.runs import append_workflow_event
from app.services.run_state import create_workflow
from app.storage import auth


DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.auth,
    pytest.mark.skipif(not DATABASE_URL, reason="isolated PostgreSQL required"),
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "database_url", DATABASE_URL)
    monkeypatch.setattr(settings, "durable_control_plane_enabled", True)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "api_keys", [])
    monkeypatch.setattr(settings, "auth_token_secret", "session-revocation-test-" + "s" * 64)
    asyncio.run(close_database())
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")

    @asynccontextmanager
    async def lifespan(app):
        yield
        await close_database()

    app = FastAPI(lifespan=lifespan)
    app.include_router(login_router)
    app.include_router(tasks_router)
    app.websocket("/ws/{session_id}")(websocket_endpoint)
    app.websocket("/ws/tasks/{workflow_run_id}")(durable_task_websocket)
    with TestClient(app) as instance:
        yield instance


def login(client):
    phone = "20261020" + str(secrets.randbelow(10**10)).zfill(10)
    password = secrets.token_urlsafe(24)
    client.portal.call(auth.create_user, phone, password, "integration_test")
    response = client.post(
        "/api/auth/login/password", json={"phone": phone, "password": password},
    )
    assert response.status_code == 200
    result = response.json()
    token = result["token"]
    protocol = "cad-agent-auth." + base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
    return result["user"], {"Authorization": "Bearer " + token}, protocol


@pytest.mark.parametrize("operation", ["logout", "refresh"])
def test_modeling_socket_rejects_requests_after_session_revocation(client, operation):
    _, headers, protocol = login(client)
    with client.websocket_connect("/ws/revocation-" + uuid4().hex, subprotocols=[protocol]) as ws:
        assert client.post("/api/auth/" + operation, headers=headers).status_code == 200
        assert client.get("/api/auth/me", headers=headers).status_code == 401
        ws.send_json({
            "type": "recover_submission",
            "panel_id": "revocation",
            "idempotency_key": uuid4().hex,
        })
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 4003


def test_task_stream_stops_delivering_events_after_logout(client):
    user, headers, protocol = login(client)
    principal = user_principal(user["id"])

    async def create_pending_task():
        async with tenant_transaction(principal.tenant_id, principal.principal_id) as connection:
            project_id = uuid4()
            await create_project(
                connection, project_id=project_id, tenant_id=principal.tenant_id,
                creator_principal_id=principal.principal_id, name="Revocation probe",
                slug=str(project_id),
            )
            return await create_workflow(
                connection, tenant_id=principal.tenant_id, project_id=project_id,
                requested_by_principal_id=principal.principal_id,
                kind="mcad.generate", idempotency_key=uuid4().hex, request_payload={},
            )

    run = client.portal.call(create_pending_task)
    # Start after the already persisted creation event. Only a post-revocation
    # event could arrive after the initial snapshot.
    path = f"/ws/tasks/{run.workflow_id}?after_sequence=1"
    with client.websocket_connect(path, subprotocols=[protocol]) as ws:
        assert ws.receive_json()["type"] == "task_snapshot"
        assert client.post("/api/auth/logout", headers=headers).status_code == 200
        assert client.get("/api/auth/me", headers=headers).status_code == 401

        async def append_after_logout():
            async with tenant_transaction(principal.tenant_id, principal.principal_id) as connection:
                await append_workflow_event(
                    connection, tenant_id=principal.tenant_id,
                    workflow_id=run.workflow_id, event_type="audit.revocation_probe",
                    payload={"record": "must not reach a revoked session"},
                )

        client.portal.call(append_after_logout)
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 4003
