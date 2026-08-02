"""Real atomic-cutover HTTP contract through every durable dependency."""
from __future__ import annotations

import asyncio
import hashlib
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

from app.config import settings
from app.db import close_database, get_database_engine, tenant_transaction
from app.domain.identity import api_key_principal
from app.execution.composition import get_execution_backend
from app.main import create_app
from app.object_store import delete_object, reset_object_store_client
from app.principal_context import bind_principal
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.repositories.revisions import create_initial_branch
from app.services.durable_submission import ensure_workspace_identity
from app.storage import postgres_history
from app.temporal_client import get_temporal_client, reset_temporal_client
from app.workers.workflow_worker import build_workflow_worker


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
RUN_EXTERNAL = (
    os.environ.get("CAD_AGENT_TEST_OBJECT_STORE") == "1"
    and os.environ.get("CAD_AGENT_TEST_TEMPORAL") == "1"
    and bool(os.environ.get("TEMPORAL_TARGET"))
)
RUN_LLM = os.environ.get("CAD_AGENT_TEST_LLM") == "1"
OWNER_KEY = "task10-api-owner-key"
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
        "durable_control_plane_enabled":
            settings.durable_control_plane_enabled,
        "durable_api_cutover_enabled":
            settings.durable_api_cutover_enabled,
        "auth_required": settings.auth_required,
        "api_keys": settings.api_keys,
        "temporal_target": settings.temporal_target,
        "sandbox_runtime": settings.sandbox_runtime,
        "sandbox_command": settings.sandbox_command,
        "sandbox_image": settings.sandbox_image,
    }
    settings.database_url = TEST_DATABASE_URL
    settings.durable_control_plane_enabled = True
    settings.durable_api_cutover_enabled = True
    settings.auth_required = True
    settings.api_keys = [OWNER_KEY]
    settings.temporal_target = os.environ["TEMPORAL_TARGET"]
    settings.sandbox_runtime = os.environ.get("SANDBOX_RUNTIME", "podman")
    settings.sandbox_command = os.environ.get(
        "SANDBOX_COMMAND",
        "podman",
    )
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
    for key, value in original.items():
        setattr(settings, key, value)


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
                            "UNION SELECT staging_object_key "
                            "FROM artifact_uploads"
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
                    "task_events, execution_attempts, step_runs, "
                    "workflow_runs, usage_meter_entries, audit_records, "
                    "project_memberships, projects, tenant_memberships, "
                    "principals, tenants CASCADE"
                )
            )

    await clean()
    yield
    await clean()


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
            name="Task 10 API matrix",
            slug=f"task10-{project_id.hex[:12]}",
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
        "DURABLE_API_CUTOVER_ENABLED": "true",
        "AUTH_REQUIRED": "true",
        "AUTH_TOKEN_SECRET": "task10-test-" + ("s" * 64),
        "API_KEYS": json.dumps([OWNER_KEY]),
        "OBJECT_STORE_ENDPOINT_URL": os.environ[
            "OBJECT_STORE_ENDPOINT_URL"
        ],
        "OBJECT_STORE_ACCESS_KEY": os.environ[
            "OBJECT_STORE_ACCESS_KEY"
        ],
        "OBJECT_STORE_SECRET_KEY": os.environ[
            "OBJECT_STORE_SECRET_KEY"
        ],
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
                    f"http://127.0.0.1:{port}/ready",
                    timeout=1,
                )
                if response.status_code == 200:
                    assert response.json()["status"] == "ready"
                    return process
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    process.kill()
    await process.wait()
    raise AssertionError("FastAPI did not become ready")


async def _stop_api(process) -> None:
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


@pytest.mark.asyncio(loop_scope="module")
async def test_execute_replay_download_review_and_stale_base_matrix():
    owner = api_key_principal(OWNER_KEY)
    session_id = f"task10-history-{uuid4()}"
    panel_id = f"task10-panel-{uuid4()}"
    bind_principal(owner)
    workspace = await ensure_workspace_identity(
        owner,
        session_id=session_id,
        panel_id=panel_id,
        title="持久历史真实链路",
        user_id=None,
    )
    project_id = workspace.project_id
    temporal = await get_temporal_client()
    transport = httpx.ASGITransport(app=create_app())
    headers = {"Authorization": f"Bearer {OWNER_KEY}"}
    idempotency_key = f"api-matrix-{uuid4()}"
    request = {
        "project_id": str(project_id),
        "branch_id": str(workspace.branch_id),
        "expected_base_revision_id": str(workspace.head_revision_id),
        "idempotency_key": idempotency_key,
        "code": (
            "import cadquery as cq\n"
            "result = cq.Workplane('XY').box(24, 12, 6)\n"
        ),
        "output_formats": ["step", "stl"],
    }

    async with (
        build_workflow_worker(temporal),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client,
    ):
        created = await client.post(
            "/api/execute",
            headers=headers,
            json=request,
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["success"] is True
        assert body["task_status"] == "succeeded"
        assert body["workflow_run_id"] == body["request_id"]
        assert body["change_set_id"]
        assert body["code"] == request["code"]
        assert set(body["files"]) == {"step", "stl"}

        snapshot = await client.get(
            f"/api/tasks/{body['workflow_run_id']}/snapshot",
            headers=headers,
        )
        assert snapshot.status_code == 200, snapshot.text
        snapshot_body = snapshot.json()
        assert snapshot_body["status"] == "succeeded"
        assert len(snapshot_body["artifacts"]) == 2
        assert all(
            artifact["download_url"].startswith("/api/files/")
            for artifact in snapshot_body["artifacts"]
        )

        for url in body["files"].values():
            downloaded = await client.get(url, headers=headers)
            assert downloaded.status_code == 200, downloaded.text
            assert downloaded.content
            assert downloaded.headers["etag"].startswith('"')

        replay = await client.post(
            "/api/execute",
            headers=headers,
            json=request,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["workflow_run_id"] == body["workflow_run_id"]

        accepted = await client.post(
            f"/api/change-sets/{body['change_set_id']}/accept",
            headers=headers,
            json={"note": "真实 API 矩阵确认"},
        )
        assert accepted.status_code == 200, accepted.text
        committed = await client.post(
            f"/api/change-sets/{body['change_set_id']}/commit",
            headers=headers,
        )
        assert committed.status_code == 200, committed.text
        assert committed.json()["status"] == "committed"

        replay_after_commit = await client.post(
            "/api/execute",
            headers=headers,
            json=request,
        )
        assert replay_after_commit.status_code == 200
        assert (
            replay_after_commit.json()["workflow_run_id"]
            == body["workflow_run_id"]
        )

        stale_request = {
            **request,
            "idempotency_key": f"stale-{uuid4()}",
        }
        stale = await client.post(
            "/api/execute",
            headers=headers,
            json=stale_request,
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["type"] == "RevisionConflict"

    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        counts = (
            await connection.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM workflow_runs) AS workflows,
                      (SELECT count(*) FROM execution_attempts) AS attempts,
                      (SELECT count(*) FROM artifacts) AS artifacts
                    """
                )
            )
        ).mappings().one()
    assert dict(counts) == {
        "workflows": 1,
        "attempts": 1,
        "artifacts": 2,
    }
    bind_principal(owner)
    panels = await postgres_history.list_panels(session_id)
    assert len(panels) == 1
    assert panels[0]["id"] == panel_id
    assert panels[0]["current_code"] == request["code"]
    assert panels[0]["active_workflow_run_id"] == body["workflow_run_id"]
    assert panels[0]["active_workflow_status"] == "succeeded"
    messages = await postgres_history.get_messages(panel_id)
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
    ]
    assert messages[1]["result"]["workflow_run_id"] == body[
        "workflow_run_id"
    ]
    assert messages[1]["result"]["code"] == request["code"]
    assert set(messages[1]["result"]["files"]) == {"step", "stl"}


@pytest.mark.asyncio(loop_scope="module")
async def test_engineering_check_is_durable_downloadable_and_idempotent():
    owner = api_key_principal(OWNER_KEY)
    bind_principal(owner)
    workspace = await ensure_workspace_identity(
        owner,
        session_id=f"task11-check-{uuid4()}",
        panel_id=f"task11-check-panel-{uuid4()}",
        title="持久工程检查真实链路",
        user_id=None,
    )
    temporal = await get_temporal_client()
    transport = httpx.ASGITransport(app=create_app())
    headers = {"Authorization": f"Bearer {OWNER_KEY}"}
    execute_request = {
        "project_id": str(workspace.project_id),
        "branch_id": str(workspace.branch_id),
        "expected_base_revision_id": str(workspace.head_revision_id),
        "idempotency_key": f"durable-check-source-{uuid4()}",
        "code": (
            "import cadquery as cq\n"
            "result = cq.Workplane('XY').box(30, 20, 10)\n"
        ),
        "output_formats": ["step", "stl"],
    }
    check_request = {
        "code": execute_request["code"],
        "description": "真实持久工程检查",
        "process": "CNC",
        "material": "aluminum",
    }

    async with (
        build_workflow_worker(temporal),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            timeout=180,
        ) as client,
    ):
        generated = await client.post(
            "/api/execute",
            headers=headers,
            json=execute_request,
        )
        assert generated.status_code == 200, generated.text
        source = generated.json()
        assert source["success"] is True

        checked = await client.post(
            f"/api/analyze/{source['workflow_run_id']}",
            headers=headers,
            json=check_request,
        )
        assert checked.status_code == 200, checked.text
        check_workflow_id = checked.headers["x-workflow-run-id"]
        result = checked.json()
        assert result["step_analysis"]["available"] is True
        assert result["geometry"]["volume"] == pytest.approx(6000.0)

        snapshot = await client.get(
            f"/api/tasks/{check_workflow_id}/snapshot",
            headers=headers,
        )
        assert snapshot.status_code == 200, snapshot.text
        snapshot_body = snapshot.json()
        assert snapshot_body["kind"] == "mcad.check"
        assert snapshot_body["status"] == "succeeded"
        assert len(snapshot_body["steps"]) == 1
        assert snapshot_body["steps"][0]["status"] == "succeeded"
        assert len(snapshot_body["steps"][0]["attempts"]) == 1
        assert (
            snapshot_body["steps"][0]["attempts"][0]["status"]
            == "succeeded"
        )
        assert len(snapshot_body["artifacts"]) == 1
        report_metadata = snapshot_body["artifacts"][0]
        assert report_metadata["artifact_kind"] == "dfm_report"

        report = await client.get(
            report_metadata["download_url"],
            headers=headers,
        )
        assert report.status_code == 200, report.text
        report_sha = hashlib.sha256(report.content).hexdigest()
        assert report.headers["etag"] == f'"{report_sha}"'
        report_body = report.json()
        assert report_body["schema_version"] == "dfm-report.v1"
        assert report_body["workflow_run_id"] == check_workflow_id
        assert (
            report_body["source_workflow_run_id"]
            == source["workflow_run_id"]
        )
        assert report_body["analysis"] == result

        replay = await client.post(
            f"/api/analyze/{source['workflow_run_id']}",
            headers=headers,
            json=check_request,
        )
        assert replay.status_code == 200, replay.text
        assert replay.headers["x-workflow-run-id"] == check_workflow_id
        assert replay.json() == result

    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        counts = (
            await connection.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM workflow_runs
                       WHERE kind='mcad.check') AS check_workflows,
                      (SELECT count(*) FROM execution_attempts
                       WHERE workflow_run_id=:workflow_id) AS attempts,
                      (SELECT count(*) FROM artifacts
                       WHERE workflow_run_id=:workflow_id) AS reports
                    """
                ),
                {"workflow_id": check_workflow_id},
            )
        ).mappings().one()
    assert dict(counts) == {
        "check_workflows": 1,
        "attempts": 1,
        "reports": 1,
    }


@pytest.mark.asyncio(loop_scope="module")
async def test_conversational_ws_submits_once_then_replays_after_disconnect():
    owner, project_id, initial = await _seed_project()
    temporal = await get_temporal_client()
    port = _available_port()
    api = await _start_api(port)
    workflow_id = None
    received_sequences: list[int] = []
    terminal_snapshot = None
    try:
        async with build_workflow_worker(temporal):
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/ws/task10-session"
                f"?token={OWNER_KEY}",
            ) as socket_connection:
                await socket_connection.send(
                    json.dumps(
                        {
                            "type": "execute_code",
                            "panel_id": "task10-panel",
                            "project_id": str(project_id),
                            "branch_id": str(initial.branch_id),
                            "expected_base_revision_id": str(
                                initial.revision_id
                            ),
                            "idempotency_key": f"ws-submit-{uuid4()}",
                            "code": (
                                "import cadquery as cq\n"
                                "result = cq.Workplane('XY').box(8, 6, 4)\n"
                            ),
                        }
                    )
                )
                submitted = json.loads(
                    await asyncio.wait_for(
                        socket_connection.recv(),
                        timeout=10,
                    )
                )
                assert submitted["type"] == "task_submitted"
                workflow_id = submitted["data"]["workflow_run_id"]

            async with websockets.connect(
                f"ws://127.0.0.1:{port}/ws/tasks/{workflow_id}"
                f"?token={OWNER_KEY}&after_sequence=0",
            ) as replay:
                while True:
                    message = json.loads(
                        await asyncio.wait_for(replay.recv(), timeout=60)
                    )
                    if message["type"] == "task_event":
                        received_sequences.append(
                            message["data"]["sequence"]
                        )
                    elif message["type"] == "task_snapshot":
                        terminal_snapshot = message["data"]
                    elif message["type"] == "task_stream_complete":
                        assert message["data"]["status"] == "succeeded"
                        break
    finally:
        await _stop_api(api)

    assert workflow_id is not None
    assert received_sequences == list(
        range(1, len(received_sequences) + 1)
    )
    assert terminal_snapshot["status"] == "succeeded"
    assert len(terminal_snapshot["artifacts"]) == 2
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        attempt_count = await connection.scalar(
            text(
                "SELECT count(*) FROM execution_attempts "
                "WHERE workflow_run_id=:workflow_id"
            ),
            {"workflow_id": workflow_id},
        )
        legacy_message_count = await connection.scalar(
            text("SELECT count(*) FROM workspace_messages")
        )
    assert attempt_count == 1
    assert legacy_message_count == 0


@pytest.mark.skipif(
    not RUN_LLM,
    reason="set CAD_AGENT_TEST_LLM=1 for the real planner/codegen provider",
)
@pytest.mark.asyncio(loop_scope="module")
async def test_real_generate_runs_llm_inside_temporal_before_podman():
    owner, project_id, initial = await _seed_project()
    temporal = await get_temporal_client()
    transport = httpx.ASGITransport(app=create_app())
    headers = {"Authorization": f"Bearer {OWNER_KEY}"}
    request = {
        "project_id": str(project_id),
        "branch_id": str(initial.branch_id),
        "expected_base_revision_id": str(initial.revision_id),
        "idempotency_key": f"real-generate-{uuid4()}",
        "prompt": (
            "创建一个无孔、无圆角、实心的长方体测试块："
            "长度 20 mm，宽度 10 mm，高度 5 mm。"
            "三个尺寸均已确认，不需要补充信息。"
        ),
        "output_formats": ["step", "stl"],
    }

    async with (
        build_workflow_worker(temporal),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            timeout=180,
        ) as client,
    ):
        generated = await client.post(
            "/api/generate",
            headers=headers,
            json=request,
        )
        failure_evidence = None
        if generated.status_code != 200:
            failed_body = generated.json()
            failed_workflow_id = failed_body.get("workflow_run_id")
            if failed_workflow_id:
                failed_events = await client.get(
                    f"/api/tasks/{failed_workflow_id}/events",
                    headers=headers,
                )
                failed_snapshot = await client.get(
                    f"/api/tasks/{failed_workflow_id}/snapshot",
                    headers=headers,
                )
                failure_evidence = {
                    "steps": failed_snapshot.json().get("steps"),
                    "events": [
                        {
                            "event_type": event["event_type"],
                            "payload": event["payload"],
                        }
                        for event in failed_events.json().get("events", [])
                        if (
                            "failed" in event["event_type"]
                            or event["event_type"].startswith("source.")
                        )
                    ],
                }
        assert generated.status_code == 200, (
            generated.text,
            failure_evidence,
        )
        body = generated.json()
        assert body["success"] is True
        assert body["code"]
        assert body["plan"]
        assert set(body["files"]) == {"step", "stl"}

        events = await client.get(
            f"/api/tasks/{body['workflow_run_id']}/events",
            headers=headers,
        )
        assert events.status_code == 200, events.text
        event_types = [
            event["event_type"] for event in events.json()["events"]
        ]
        assert "source.preparation_started" in event_types
        assert "source.prepared" in event_types
        assert "attempt.succeeded" in event_types

    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        workflow = (
            await connection.execute(
                text(
                    """
                    SELECT status, request_payload
                    FROM workflow_runs
                    """
                )
            )
        ).mappings().one()
        attempt_count = await connection.scalar(
            text("SELECT count(*) FROM execution_attempts")
        )
    assert workflow["status"] == "succeeded"
    assert workflow["request_payload"]["preparation"]["operation"] == (
        "generate"
    )
    assert attempt_count == 1
