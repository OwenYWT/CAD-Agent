"""Real persisted WebSocket replay, reconnect, auth, and retention boundaries."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import websockets
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from websockets.exceptions import InvalidStatus

from app.config import settings
from app.db import close_database, get_database_engine, tenant_transaction
from app.domain.identity import api_key_principal
from app.execution.composition import get_execution_backend
from app.object_store import delete_object, reset_object_store_client
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.repositories.revisions import create_initial_branch
from app.temporal_client import get_temporal_client, reset_temporal_client
from app.workers.workflow_worker import build_workflow_worker
from app.workflows.temporal import McadExecutionRequest, start_mcad_workflow


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
RUN_EXTERNAL = (
    os.environ.get("CAD_AGENT_TEST_OBJECT_STORE") == "1"
    and os.environ.get("CAD_AGENT_TEST_TEMPORAL") == "1"
    and bool(os.environ.get("TEMPORAL_TARGET"))
)
OWNER_KEY = "task9-ws-owner-api-key"
OTHER_KEY = "task9-ws-other-api-key"
pytestmark = [
    pytest.mark.skipif(
        not TEST_DATABASE_URL or not RUN_EXTERNAL,
        reason="real PostgreSQL, MinIO, Temporal, and Podman are required",
    ),
    pytest.mark.slow,
]


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    if not TEST_DATABASE_URL or not RUN_EXTERNAL:
        yield
        return
    original = {
        "database_url": settings.database_url,
        "durable": settings.durable_control_plane_enabled,
        "temporal_target": settings.temporal_target,
        "sandbox_runtime": settings.sandbox_runtime,
        "sandbox_command": settings.sandbox_command,
        "sandbox_image": settings.sandbox_image,
    }
    settings.database_url = TEST_DATABASE_URL
    settings.durable_control_plane_enabled = True
    settings.temporal_target = os.environ["TEMPORAL_TARGET"]
    settings.sandbox_runtime = os.environ.get("SANDBOX_RUNTIME", "podman")
    settings.sandbox_command = os.environ.get("SANDBOX_COMMAND", "podman")
    settings.sandbox_image = os.environ.get(
        "SANDBOX_IMAGE",
        "localhost/cad-agent-sandbox:m0-unified",
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    reset_temporal_client()
    reset_object_store_client()
    get_execution_backend.cache_clear()
    yield
    settings.database_url = original["database_url"]
    settings.durable_control_plane_enabled = original["durable"]
    settings.temporal_target = original["temporal_target"]
    settings.sandbox_runtime = original["sandbox_runtime"]
    settings.sandbox_command = original["sandbox_command"]
    settings.sandbox_image = original["sandbox_image"]


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def lifecycle(migrated_database):
    yield
    await close_database()
    reset_temporal_client()
    reset_object_store_client()
    get_execution_backend.cache_clear()


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean_control_plane():
    if not TEST_DATABASE_URL or not RUN_EXTERNAL:
        yield
        return

    async def clean() -> None:
        async with get_database_engine().begin() as connection:
            object_keys = list(
                (
                    await connection.execute(
                        text(
                            "SELECT object_key FROM artifacts "
                            "UNION SELECT staging_object_key FROM artifact_uploads"
                        )
                    )
                ).scalars()
            )
        for key in object_keys:
            await delete_object(key)
        async with get_database_engine().begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE artifacts, artifact_uploads, change_sets, "
                    "project_revisions, project_branches, outbox_messages, "
                    "task_events, execution_attempts, step_runs, workflow_runs, "
                    "usage_meter_entries, audit_records, project_memberships, "
                    "projects, tenant_memberships, principals, tenants CASCADE"
                )
            )

    await clean()
    yield
    await clean()


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _start_api(port: int):
    env = {
        **os.environ,
        "APP_ENVIRONMENT": "test",
        "DATABASE_URL": TEST_DATABASE_URL,
        "DURABLE_CONTROL_PLANE_ENABLED": "true",
        "DURABLE_API_CUTOVER_ENABLED": "false",
        "AUTH_REQUIRED": "true",
        "AUTH_TOKEN_SECRET": "task9-test-" + ("s" * 64),
        "API_KEYS": json.dumps([OWNER_KEY, OTHER_KEY]),
        "OBJECT_STORE_ENDPOINT_URL": os.environ["OBJECT_STORE_ENDPOINT_URL"],
        "OBJECT_STORE_ACCESS_KEY": os.environ["OBJECT_STORE_ACCESS_KEY"],
        "OBJECT_STORE_SECRET_KEY": os.environ["OBJECT_STORE_SECRET_KEY"],
        "OBJECT_STORE_BUCKET": os.environ["OBJECT_STORE_BUCKET"],
        "TEMPORAL_TARGET": os.environ["TEMPORAL_TARGET"],
        "SANDBOX_RUNTIME": os.environ.get("SANDBOX_RUNTIME", "podman"),
        "SANDBOX_COMMAND": os.environ.get("SANDBOX_COMMAND", "podman"),
        "SANDBOX_IMAGE": os.environ.get(
            "SANDBOX_IMAGE",
            "localhost/cad-agent-sandbox:m0-unified",
        ),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        cwd=str(ROOT),
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    deadline = asyncio.get_running_loop().time() + 20
    async with httpx.AsyncClient() as client:
        while asyncio.get_running_loop().time() < deadline:
            if process.returncode is not None:
                raise AssertionError("FastAPI exited during startup")
            try:
                response = await client.get(
                    f"http://127.0.0.1:{port}/health",
                    timeout=1,
                )
                if response.status_code == 200:
                    return process
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    process.kill()
    await process.wait()
    raise AssertionError("FastAPI did not start")


async def _stop_api(process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def _seed_project():
    owner = api_key_principal(OWNER_KEY)
    project_id = uuid4()
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        await ensure_principal(connection, owner)
        await create_project(
            connection,
            project_id=project_id,
            tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id,
            name="Task 9 WebSocket project",
            slug=f"task9-ws-{project_id.hex[:12]}",
        )
        initial = await create_initial_branch(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            created_by_principal_id=owner.principal_id,
            branch_name="main",
            initial_manifest={"state": "empty"},
        )
    return owner, project_id, initial


def _box_execution() -> McadExecutionRequest:
    return McadExecutionRequest(
        step_key="model",
        kind="mcad_model",
        operation="generate",
        mode="3d",
        source_code=(
            "import cadquery as cq\n"
            "result = cq.Workplane('XY').box(18, 12, 4)\n"
        ),
        timeout_seconds=60,
    )


async def _wait_for_status(owner, workflow_id, expected: set[str], timeout=40):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            status = await connection.scalar(
                text("SELECT status FROM workflow_runs WHERE id=:id"),
                {"id": workflow_id},
            )
        if status in expected:
            return status
        await asyncio.sleep(0.1)
    raise AssertionError(f"workflow did not reach {expected}")


async def _recv_json(socket):
    return json.loads(await asyncio.wait_for(socket.recv(), timeout=20))


def _auth_protocol(token: str) -> str:
    encoded = base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
    return f"cad-agent-auth.{encoded}"


@pytest.mark.asyncio(loop_scope="module")
async def test_real_websocket_replay_reconnect_slow_consumer_auth_and_retention():
    owner, project_id, initial = await _seed_project()
    temporal = await get_temporal_client()
    port = _available_port()
    api = await _start_api(port)
    headers = {"Authorization": f"Bearer {OWNER_KEY}"}
    try:
        async with build_workflow_worker(temporal):
            workflow_id, handle = await start_mcad_workflow(
                tenant_id=owner.tenant_id,
                project_id=project_id,
                principal_id=owner.principal_id,
                branch_id=initial.branch_id,
                expected_base_revision_id=initial.revision_id,
                kind="generate",
                idempotency_key=f"task9-ws-{project_id}",
                objective="生成 18×12×4 mm 实体并验证事件回放",
                primary=_box_execution(),
            )
            assert await _wait_for_status(
                owner,
                workflow_id,
                {"waiting_confirmation"},
            ) == "waiting_confirmation"

            unauthorized_uri = (
                f"ws://127.0.0.1:{port}/ws/tasks/{workflow_id}"
                "?after_sequence=0"
            )
            with pytest.raises(InvalidStatus):
                async with websockets.connect(
                    unauthorized_uri,
                    subprotocols=[_auth_protocol(OTHER_KEY)],
                ):
                    pass

            uri = (
                f"ws://127.0.0.1:{port}/ws/tasks/{workflow_id}"
                "?after_sequence=0"
            )
            first_sequences: list[int] = []
            async with websockets.connect(
                uri,
                subprotocols=[_auth_protocol(OWNER_KEY)],
            ) as socket:
                assert socket.subprotocol == _auth_protocol(OWNER_KEY)
                snapshot = await _recv_json(socket)
                assert snapshot["type"] == "task_snapshot"
                assert snapshot["data"]["status"] == "waiting_confirmation"
                while len(first_sequences) < 3:
                    message = await _recv_json(socket)
                    if message["type"] == "task_event":
                        first_sequences.append(message["data"]["sequence"])
            assert first_sequences == sorted(first_sequences)
            cursor = first_sequences[-1]

            reconnect_uri = (
                f"ws://127.0.0.1:{port}/ws/tasks/{workflow_id}"
                f"?after_sequence={cursor}"
            )
            replay_sequences: list[int] = []
            terminal_snapshot_seen = False
            async with websockets.connect(
                reconnect_uri,
                subprotocols=[_auth_protocol(OWNER_KEY)],
            ) as socket:
                snapshot = await _recv_json(socket)
                assert snapshot["type"] == "task_snapshot"
                # Deliberately stop reading while the server tails the persisted
                # log. The bridge must backpressure rather than drop events.
                await asyncio.sleep(1)
                async with httpx.AsyncClient() as client:
                    accepted = await client.post(
                        f"http://127.0.0.1:{port}/api/tasks/"
                        f"{workflow_id}/confirmation",
                        headers=headers,
                        json={"accepted": True, "note": "WebSocket 联调确认"},
                    )
                    assert accepted.status_code == 200, accepted.text
                while True:
                    message = await _recv_json(socket)
                    if message["type"] == "task_event":
                        replay_sequences.append(message["data"]["sequence"])
                    elif (
                        message["type"] == "task_snapshot"
                        and message["data"]["status"] == "succeeded"
                    ):
                        terminal_snapshot_seen = True
                        assert message["data"]["change_set"]["status"] == (
                            "committed"
                        )
                    elif message["type"] == "task_stream_complete":
                        assert message["data"]["status"] == "succeeded"
                        break

            result = await asyncio.wait_for(handle.result(), timeout=60)
            assert result["status"] == "succeeded"
            assert terminal_snapshot_seen
            assert replay_sequences
            assert min(replay_sequences) > cursor
            assert replay_sequences == sorted(replay_sequences)
            assert not set(first_sequences).intersection(replay_sequences)
            assert len(replay_sequences) == len(set(replay_sequences))

            async with httpx.AsyncClient() as client:
                snapshot = await client.get(
                    f"http://127.0.0.1:{port}/api/tasks/"
                    f"{workflow_id}/snapshot",
                    headers=headers,
                )
                assert snapshot.status_code == 200
                last_sequence = snapshot.json()["last_event_sequence"]
                assert replay_sequences[-1] == last_sequence
                empty = await client.get(
                    f"http://127.0.0.1:{port}/api/tasks/{workflow_id}/events",
                    params={"after_sequence": last_sequence},
                    headers=headers,
                )
                assert empty.status_code == 200
                assert empty.json()["events"] == []

            retained_workflow_id = uuid4()
            async with tenant_transaction(
                owner.tenant_id,
                owner.principal_id,
            ) as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO workflow_runs (
                            id, tenant_id, project_id,
                            requested_by_principal_id, kind, status,
                            idempotency_key, request_payload_hash,
                            request_payload, last_event_sequence
                        ) VALUES (
                            :id, :tenant, :project, :principal, 'retention',
                            'failed', :key, :hash, '{}'::jsonb, 2
                        )
                        """
                    ),
                    {
                        "id": retained_workflow_id,
                        "tenant": owner.tenant_id,
                        "project": project_id,
                        "principal": owner.principal_id,
                        "key": f"retention-{retained_workflow_id}",
                        "hash": "0" * 64,
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO task_events (
                            id, tenant_id, workflow_run_id, sequence,
                            event_type, payload
                        ) VALUES (
                            :id, :tenant, :workflow, 2,
                            'retained.boundary', '{}'::jsonb
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "tenant": owner.tenant_id,
                        "workflow": retained_workflow_id,
                    },
                )
            async with httpx.AsyncClient() as client:
                expired = await client.get(
                    f"http://127.0.0.1:{port}/api/tasks/"
                    f"{retained_workflow_id}/events",
                    params={"after_sequence": 0},
                    headers=headers,
                )
                assert expired.status_code == 410
                assert expired.json()["detail"]["code"] == "event_cursor_expired"
    finally:
        await _stop_api(api)
