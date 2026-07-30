"""Real Temporal + PostgreSQL + MinIO + Podman MCAD workflow tests."""
from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import socket
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from temporalio.client import WorkflowFailureError

from app.config import settings
from app.db import close_database, get_database_engine, tenant_transaction
from app.domain.identity import user_principal
from app.execution.composition import get_execution_backend
from app.object_store import delete_object, get_object, reset_object_store_client
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.repositories.revisions import (
    compare_and_swap_branch_head,
    create_candidate_change_set,
    create_initial_branch,
)
from app.temporal_client import get_temporal_client, reset_temporal_client
from app.workers.workflow_worker import build_workflow_worker
from app.workflows.temporal import (
    McadExecutionRequest,
    cancel_mcad_workflow,
    confirm_mcad_workflow,
    start_mcad_workflow,
)


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
RUN_EXTERNAL = (
    os.environ.get("CAD_AGENT_TEST_OBJECT_STORE") == "1"
    and os.environ.get("CAD_AGENT_TEST_TEMPORAL") == "1"
    and bool(os.environ.get("TEMPORAL_TARGET"))
)
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
    settings.database_url = TEST_DATABASE_URL
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def external_lifecycle(migrated_database):
    if not TEST_DATABASE_URL or not RUN_EXTERNAL:
        yield
        return
    original = {
        "database_url": settings.database_url,
        "temporal_target": settings.temporal_target,
        "sandbox_runtime": settings.sandbox_runtime,
        "sandbox_command": settings.sandbox_command,
        "sandbox_image": settings.sandbox_image,
    }
    settings.database_url = TEST_DATABASE_URL
    settings.temporal_target = os.environ["TEMPORAL_TARGET"]
    settings.sandbox_runtime = os.environ.get("SANDBOX_RUNTIME", "podman")
    settings.sandbox_command = os.environ.get("SANDBOX_COMMAND", "podman")
    settings.sandbox_image = os.environ.get(
        "SANDBOX_IMAGE",
        "localhost/cad-agent-sandbox:m0-unified",
    )
    reset_temporal_client()
    reset_object_store_client()
    get_execution_backend.cache_clear()
    yield
    await close_database()
    reset_temporal_client()
    reset_object_store_client()
    get_execution_backend.cache_clear()
    for key, value in original.items():
        setattr(settings, key, value)


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


async def _seed_project(label: str):
    owner = user_principal(f"temporal-{label}-{uuid4()}")
    project_id = uuid4()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        await ensure_principal(connection, owner)
        await create_project(
            connection,
            project_id=project_id,
            tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id,
            name=f"Temporal {label}",
            slug=f"temporal-{project_id.hex[:12]}",
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


async def _wait_for_status(owner, workflow_id, expected: set[str], timeout=30):
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
    raise AssertionError(
        f"workflow {workflow_id} did not reach {sorted(expected)}"
    )


async def _snapshot(owner, workflow_id):
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        workflow = (
            await connection.execute(
                text(
                    """
                    SELECT status, last_event_sequence
                    FROM workflow_runs WHERE id=:id
                    """
                ),
                {"id": workflow_id},
            )
        ).mappings().one()
        attempts = (
            await connection.execute(
                text(
                    """
                    SELECT id, status, attempt_number, error_code
                    FROM execution_attempts
                    WHERE workflow_run_id=:id
                    ORDER BY created_at
                    """
                ),
                {"id": workflow_id},
            )
        ).mappings().all()
        artifacts = (
            await connection.execute(
                text(
                    """
                    SELECT filename, object_key, size_bytes, sha256
                    FROM artifacts WHERE workflow_run_id=:id
                    ORDER BY filename
                    """
                ),
                {"id": workflow_id},
            )
        ).mappings().all()
        change_set = (
            await connection.execute(
                text(
                    """
                    SELECT c.status, c.candidate_revision_id,
                           c.validation_summary,
                           b.head_revision_id
                    FROM change_sets c
                    JOIN project_branches b ON b.id=c.branch_id
                    WHERE c.source_workflow_run_id=:id
                    """
                ),
                {"id": workflow_id},
            )
        ).mappings().one_or_none()
        events = (
            await connection.execute(
                text(
                    """
                    SELECT sequence, event_type, payload
                    FROM task_events WHERE workflow_run_id=:id
                    ORDER BY sequence
                    """
                ),
                {"id": workflow_id},
            )
        ).mappings().all()
    return {
        "workflow": workflow,
        "attempts": attempts,
        "artifacts": artifacts,
        "change_set": change_set,
        "events": events,
    }


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _start_api(env: dict[str, str], port: int):
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


def _box_execution(*, delay_seconds: int = 0) -> McadExecutionRequest:
    delay = (
        f"for _ in range({delay_seconds * 10_000_000}):\n"
        "    pass\n"
        if delay_seconds
        else ""
    )
    return McadExecutionRequest(
        step_key="model",
        kind="mcad_model",
        operation="generate",
        mode="3d",
        source_code=(
            f"{delay}import cadquery as cq\n"
            "result = cq.Workplane('XY').box(20, 10, 5)\n"
        ),
        timeout_seconds=60,
    )


def _dxf_execution() -> McadExecutionRequest:
    return McadExecutionRequest(
        step_key="export-dxf",
        kind="mcad_export",
        operation="export",
        mode="2d",
        source_code=(
            "import ezdxf\n"
            "doc = ezdxf.new()\n"
            "msp = doc.modelspace()\n"
            "msp.add_lwpolyline([(0,0),(20,0),(20,10),(0,10)], close=True)\n"
            "doc.saveas('/sandbox/output/result.dxf')\n"
        ),
        timeout_seconds=30,
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_real_plan_model_validate_confirmation_export_and_replay():
    owner, project_id, initial = await _seed_project("complete")
    client = await get_temporal_client()
    async with build_workflow_worker(client):
        workflow_id, handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="generate",
            idempotency_key=f"complete-{project_id}",
            objective="生成 20×10×5 mm 盒体并导出 DXF",
            primary=_box_execution(),
            followup=_dxf_execution(),
        )
        assert (
            await _wait_for_status(
                owner,
                workflow_id,
                {"waiting_confirmation"},
            )
            == "waiting_confirmation"
        )
        await confirm_mcad_workflow(
            workflow_id,
            accepted=True,
            note="测试确认",
        )
        result = await asyncio.wait_for(handle.result(), timeout=60)
        assert result["status"] == "succeeded"
        assert result["committed"] is True

        replay_id, replay_handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="generate",
            idempotency_key=f"complete-{project_id}",
            objective="生成 20×10×5 mm 盒体并导出 DXF",
            primary=_box_execution(),
            followup=_dxf_execution(),
        )
        assert replay_id == workflow_id
        assert (await replay_handle.result())["revision_id"] == result["revision_id"]
        await cancel_mcad_workflow(
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            workflow_run_id=workflow_id,
            reason="terminal cancellation is an idempotent no-op",
        )

    snapshot = await _snapshot(owner, workflow_id)
    assert snapshot["workflow"]["status"] == "succeeded"
    assert [row["status"] for row in snapshot["attempts"]] == [
        "succeeded",
        "succeeded",
    ]
    assert len(snapshot["artifacts"]) == 3
    assert snapshot["change_set"]["status"] == "committed"
    assert (
        snapshot["change_set"]["head_revision_id"]
        == snapshot["change_set"]["candidate_revision_id"]
    )
    assert [row["sequence"] for row in snapshot["events"]] == list(
        range(1, len(snapshot["events"]) + 1)
    )
    assert (
        sum(
            row["event_type"] == "workflow.plan_recorded"
            for row in snapshot["events"]
        )
        == 1
    )
    assert any(
        row["event_type"] == "attempt.heartbeat"
        for row in snapshot["events"]
    )
    for artifact in snapshot["artifacts"]:
        payload = await get_object(artifact["object_key"])
        assert len(payload) == artifact["size_bytes"]
        assert hashlib.sha256(payload).hexdigest() == artifact["sha256"]


@pytest.mark.asyncio(loop_scope="module")
async def test_real_confirmation_timer_rejects_without_advancing_branch():
    owner, project_id, initial = await _seed_project("timeout")
    client = await get_temporal_client()
    async with build_workflow_worker(client):
        workflow_id, handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="generate",
            idempotency_key=f"timeout-{project_id}",
            objective="生成后等待确认超时",
            primary=_box_execution(),
            confirmation_timeout_seconds=1,
        )
        result = await asyncio.wait_for(handle.result(), timeout=60)
    assert result["status"] == "timed_out"
    snapshot = await _snapshot(owner, workflow_id)
    assert snapshot["workflow"]["status"] == "timed_out"
    assert snapshot["change_set"]["status"] == "rejected"
    assert snapshot["change_set"]["head_revision_id"] == initial.revision_id


@pytest.mark.asyncio(loop_scope="module")
async def test_real_cancellation_stops_execution_and_blocks_artifacts():
    owner, project_id, initial = await _seed_project("cancel")
    client = await get_temporal_client()
    async with build_workflow_worker(client):
        workflow_id, handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="generate",
            idempotency_key=f"cancel-{project_id}",
            objective="取消长时间建模",
            primary=_box_execution(delay_seconds=20),
        )
        await _wait_for_status(owner, workflow_id, {"running"})
        await cancel_mcad_workflow(
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            workflow_run_id=workflow_id,
            reason="集成测试取消",
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result["status"] == "cancelled"
    snapshot = await _snapshot(owner, workflow_id)
    assert snapshot["workflow"]["status"] == "cancelled"
    assert snapshot["attempts"][-1]["status"] == "cancelled"
    assert snapshot["artifacts"] == []
    assert snapshot["change_set"]["status"] == "rejected"


@pytest.mark.asyncio(loop_scope="module")
async def test_nonretryable_user_code_failure_has_no_false_success():
    owner, project_id, initial = await _seed_project("user-code-failure")
    client = await get_temporal_client()
    invalid = McadExecutionRequest(
        step_key="model",
        kind="mcad_model",
        operation="generate",
        mode="3d",
        source_code="raise ValueError('invalid dimensions')\n",
    )
    async with build_workflow_worker(client):
        workflow_id, handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="generate",
            idempotency_key=f"user-code-failure-{project_id}",
            objective="验证用户代码失败不会显示成功",
            primary=invalid,
        )
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), timeout=30)
    snapshot = await _snapshot(owner, workflow_id)
    assert snapshot["workflow"]["status"] == "failed"
    assert len(snapshot["attempts"]) == 1
    assert snapshot["attempts"][0]["status"] == "failed"
    assert snapshot["attempts"][0]["error_code"] == "user_code_failed"
    assert snapshot["artifacts"] == []
    assert snapshot["change_set"]["status"] == "pending_review"
    assert snapshot["change_set"]["validation_summary"]["status"] == "failed"
    assert snapshot["change_set"]["head_revision_id"] == initial.revision_id


@pytest.mark.asyncio(loop_scope="module")
async def test_stale_base_fails_persisted_workflow_without_execution():
    owner, project_id, initial = await _seed_project("stale-base")
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        sibling = await create_candidate_change_set(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            created_by_principal_id=owner.principal_id,
            idempotency_key=f"stale-sibling-{project_id}",
            objective="并发变更",
            candidate_manifest={"state": "advanced"},
        )
        assert await compare_and_swap_branch_head(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            branch_id=initial.branch_id,
            expected_head_revision_id=initial.revision_id,
            candidate_revision_id=sibling.candidate_revision_id,
        )
    client = await get_temporal_client()
    async with build_workflow_worker(client):
        workflow_id, handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="modify",
            idempotency_key=f"stale-workflow-{project_id}",
            objective="基于过期版本修改",
            primary=_box_execution(),
        )
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), timeout=30)
    snapshot = await _snapshot(owner, workflow_id)
    assert snapshot["workflow"]["status"] == "failed"
    assert snapshot["attempts"] == []
    assert snapshot["artifacts"] == []
    assert snapshot["change_set"] is None


@pytest.mark.asyncio(loop_scope="module")
async def test_worker_process_crash_retries_with_new_fenced_attempt():
    owner, project_id, initial = await _seed_project("restart")
    workflow_id, handle = await start_mcad_workflow(
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        kind="generate",
        idempotency_key=f"restart-{project_id}",
        objective="Worker 崩溃后恢复",
        primary=_box_execution(delay_seconds=8),
    )
    env = {
        **os.environ,
        "DATABASE_URL": TEST_DATABASE_URL,
        "TEMPORAL_TARGET": settings.temporal_target,
        "TEMPORAL_TASK_QUEUE": settings.temporal_task_queue,
        "SANDBOX_RUNTIME": "podman",
        "SANDBOX_COMMAND": "podman",
        "SANDBOX_IMAGE": settings.sandbox_image,
        "DURABLE_CONTROL_PLANE_ENABLED": "true",
        "AUTH_REQUIRED": "false",
    }
    api_port = _available_port()
    api = await _start_api(env, api_port)
    first = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.workers.workflow_worker",
        cwd=str(ROOT),
        env=env,
    )
    second = None
    restarted_api = None
    try:
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            snapshot = await _snapshot(owner, workflow_id)
            if snapshot["attempts"] and snapshot["attempts"][-1]["status"] == "running":
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("first worker never started an execution attempt")
        first.send_signal(signal.SIGKILL)
        await first.wait()
        api.send_signal(signal.SIGKILL)
        await api.wait()

        second = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.workers.workflow_worker",
            cwd=str(ROOT),
            env=env,
        )
        restarted_api = await _start_api(env, api_port)
        await _wait_for_status(
            owner,
            workflow_id,
            {"waiting_confirmation"},
            timeout=45,
        )
        await confirm_mcad_workflow(workflow_id, accepted=True, note="恢复后确认")
        result = await asyncio.wait_for(handle.result(), timeout=45)
        assert result["status"] == "succeeded"
    finally:
        for process in (first, second, api, restarted_api):
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

    snapshot = await _snapshot(owner, workflow_id)
    assert len(snapshot["attempts"]) == 2
    assert snapshot["attempts"][0]["status"] == "failed"
    assert snapshot["attempts"][0]["error_code"] == (
        "superseded_by_temporal_retry"
    )
    assert snapshot["attempts"][1]["status"] == "succeeded"
    assert len(snapshot["artifacts"]) == 2
    assert snapshot["change_set"]["status"] == "committed"
    assert (
        snapshot["change_set"]["head_revision_id"]
        == snapshot["change_set"]["candidate_revision_id"]
    )
