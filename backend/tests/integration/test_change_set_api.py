"""Real durable task, Change Set, revision, Temporal, MinIO, and Podman API flow."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import settings
from app.db import close_database, get_database_engine, tenant_transaction
from app.domain.identity import api_key_principal
from app.execution.composition import get_execution_backend
from app.main import create_app
from app.object_store import delete_object, reset_object_store_client
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.repositories.revisions import create_initial_branch
from app.services.event_relay import change_set_workflow_id
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
OWNER_KEY = "task9-owner-api-key"
OTHER_KEY = "task9-other-api-key"
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
        "auth_required": settings.auth_required,
        "api_keys": settings.api_keys,
        "temporal_target": settings.temporal_target,
        "sandbox_runtime": settings.sandbox_runtime,
        "sandbox_command": settings.sandbox_command,
        "sandbox_image": settings.sandbox_image,
    }
    settings.database_url = TEST_DATABASE_URL
    settings.durable_control_plane_enabled = True
    settings.auth_required = True
    settings.api_keys = [OWNER_KEY, OTHER_KEY]
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
    for key, value in original.items():
        if key == "durable":
            settings.durable_control_plane_enabled = value
        else:
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
            name="Task 9 API project",
            slug=f"task9-{project_id.hex[:12]}",
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
            "result = cq.Workplane('XY').box(24, 16, 6)\n"
        ),
        timeout_seconds=60,
    )


def _slow_execution() -> McadExecutionRequest:
    return McadExecutionRequest(
        step_key="model",
        kind="mcad_model",
        operation="generate",
        mode="3d",
        source_code=(
            "for _ in range(200000000):\n"
            "    pass\n"
            "import cadquery as cq\n"
            "result = cq.Workplane('XY').box(10, 10, 10)\n"
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


@pytest.mark.asyncio(loop_scope="module")
async def test_real_change_set_api_accepts_signals_commits_and_reads_revision():
    owner, project_id, initial = await _seed_project()
    temporal = await get_temporal_client()
    app = create_app()
    headers = {"Authorization": f"Bearer {OWNER_KEY}"}
    other_headers = {"Authorization": f"Bearer {OTHER_KEY}"}
    transport = httpx.ASGITransport(app=app)

    async with build_workflow_worker(temporal):
        workflow_id, handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="generate",
            idempotency_key=f"task9-{project_id}",
            objective="生成 24×16×6 mm 实体",
            primary=_box_execution(),
        )
        assert await _wait_for_status(
            owner,
            workflow_id,
            {"waiting_confirmation"},
        ) == "waiting_confirmation"

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            denied = await client.get(
                f"/api/tasks/{workflow_id}/snapshot",
                headers=other_headers,
            )
            assert denied.status_code == 404

            snapshot = await client.get(
                f"/api/tasks/{workflow_id}/snapshot",
                headers=headers,
            )
            assert snapshot.status_code == 200, snapshot.text
            snapshot_body = snapshot.json()
            assert snapshot_body["status"] == "waiting_confirmation"
            assert snapshot_body["steps"]
            assert snapshot_body["artifacts"]
            change_set_id = snapshot_body["change_set"]["id"]

            event_page = await client.get(
                f"/api/tasks/{workflow_id}/events?after_sequence=0&limit=2",
                headers=headers,
            )
            assert event_page.status_code == 200, event_page.text
            first_page = event_page.json()
            assert first_page["events"]
            assert len(first_page["events"]) <= 2
            assert first_page["next_cursor"] == first_page["events"][-1]["sequence"]

            detail = await client.get(
                f"/api/change-sets/{change_set_id}",
                headers=headers,
            )
            assert detail.status_code == 200, detail.text
            detail_body = detail.json()
            assert detail_body["status"] == "pending_review"
            assert detail_body["base_revision_id"] == str(initial.revision_id)
            assert detail_body["candidate_manifest"]["objective"] == (
                "生成 24×16×6 mm 实体"
            )
            assert detail_body["artifacts"]
            artifact_url = detail_body["artifacts"][0]["download_url"]
            assert artifact_url.startswith(f"/api/documents/{initial.branch_id}/artifacts/")
            artifact_download = await client.get(artifact_url, headers=headers)
            assert artifact_download.status_code == 200, artifact_download.text
            assert len(artifact_download.content) == detail_body["artifacts"][0]["size_bytes"]
            assert (await client.get(artifact_url)).status_code in {401, 403}
            assert "object_key" not in detail_body["artifacts"][0]
            assert "tenant_id" not in detail_body
            assert "idempotency_key" not in detail_body
            assert "idempotency_payload_hash" not in detail_body
            view_url = f"/api/documents/{initial.branch_id}/revisions/{detail_body['candidate_revision_id']}"
            viewed = await client.get(view_url, headers=headers)
            assert viewed.status_code == 200, viewed.text
            view = viewed.json()
            assert view["revision_id"] == detail_body["candidate_revision_id"]
            assert view["head_revision_id"] == str(initial.revision_id)
            assert view["base_state_version"] == view["head_state_version"] == 0
            assert view["review_status"] == "pending_review"
            assert view["snapshot"]["result"]["request_id"] == str(workflow_id)
            assert all(url.startswith(f"/api/documents/{initial.branch_id}/artifacts/") for url in view["snapshot"]["files"].values())
            assert (await client.get(view_url, headers=other_headers)).status_code == 404
            assert (await client.get(f"/api/documents/{initial.branch_id}/revisions/{uuid4()}", headers=headers)).status_code == 404

            # The candidate can become visible just before the persisted
            # WorkflowRun reaches waiting_confirmation. Temporal accepts an
            # early signal, so this active race window must remain signalable.
            async with tenant_transaction(
                owner.tenant_id,
                owner.principal_id,
            ) as connection:
                await connection.execute(
                    text(
                        "UPDATE workflow_runs SET status='running' WHERE id=:id"
                    ),
                    {"id": workflow_id},
                )
            assert await change_set_workflow_id(
                owner,
                UUID(change_set_id),
            ) == workflow_id
            async with tenant_transaction(
                owner.tenant_id,
                owner.principal_id,
            ) as connection:
                await connection.execute(
                    text(
                        "UPDATE workflow_runs SET status='waiting_confirmation' "
                        "WHERE id=:id"
                    ),
                    {"id": workflow_id},
                )

            accepted = await client.post(
                f"/api/change-sets/{change_set_id}/accept",
                headers=headers,
                json={"note": "尺寸与验证证据已确认"},
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["status"] == "accepted"

        result = await asyncio.wait_for(handle.result(), timeout=60)
        assert result["status"] == "succeeded"
        assert result["committed"] is True

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            final_snapshot = await client.get(
                f"/api/tasks/{workflow_id}/snapshot",
                headers=headers,
            )
            assert final_snapshot.status_code == 200
            assert final_snapshot.json()["status"] == "succeeded"

            branches = await client.get(
                f"/api/projects/{project_id}/branches",
                headers=headers,
            )
            assert branches.status_code == 200, branches.text
            branch = branches.json()[0]
            assert branch["head_revision_id"] == result["revision_id"]

            revisions = await client.get(
                f"/api/projects/{project_id}/branches/"
                f"{initial.branch_id}/revisions",
                headers=headers,
            )
            assert revisions.status_code == 200, revisions.text
            assert [item["revision_number"] for item in revisions.json()] == [2, 1]

            revision = await client.get(
                f"/api/projects/{project_id}/revisions/{result['revision_id']}",
                headers=headers,
            )
            assert revision.status_code == 200, revision.text
            revision_body = revision.json()
            assert revision_body["content_hash"]
            assert revision_body["artifacts"]
            assert all(item["sha256"] for item in revision_body["artifacts"])
            assert all(
                "object_key" not in item
                for item in revision_body["artifacts"]
            )

            rolled_back = await client.post(
                f"/api/change-sets/{change_set_id}/rollback",
                headers=headers,
                json={"note": "端到端验证回滚"},
            )
            assert rolled_back.status_code == 200, rolled_back.text
            assert rolled_back.json()["status"] == "rolled_back"
            branches_after_rollback = await client.get(
                f"/api/projects/{project_id}/branches",
                headers=headers,
            )
            assert branches_after_rollback.json()[0]["head_revision_id"] == str(
                initial.revision_id
            )


@pytest.mark.asyncio(loop_scope="module")
async def test_real_manual_accept_commit_reject_and_request_change_apis():
    owner, project_id, initial = await _seed_project()
    temporal = await get_temporal_client()
    app = create_app()
    headers = {"Authorization": f"Bearer {OWNER_KEY}"}
    transport = httpx.ASGITransport(app=app)

    async with build_workflow_worker(temporal):
        workflow_id, handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="generate",
            idempotency_key=f"manual-review-{project_id}",
            objective="生成后由独立审查接口提交",
            primary=_box_execution(),
            commit_after_confirmation=False,
        )
        assert await _wait_for_status(
            owner,
            workflow_id,
            {"waiting_confirmation"},
        ) == "waiting_confirmation"
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            confirmed = await client.post(
                f"/api/tasks/{workflow_id}/confirmation",
                headers=headers,
                json={"accepted": True, "note": "仅恢复工作流，不自动提交"},
            )
            assert confirmed.status_code == 200, confirmed.text
        result = await asyncio.wait_for(handle.result(), timeout=60)
        assert result["status"] == "succeeded"
        assert result["committed"] is False
        change_set_id = result["change_set_id"]

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            accepted = await client.post(
                f"/api/change-sets/{change_set_id}/accept",
                headers=headers,
                json={"note": "独立审查通过"},
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["status"] == "accepted"
            committed = await client.post(
                f"/api/change-sets/{change_set_id}/commit",
                headers=headers,
            )
            assert committed.status_code == 200, committed.text
            assert committed.json()["status"] == "committed"

            rolled_back = await client.post(
                f"/api/change-sets/{change_set_id}/rollback",
                headers=headers,
                json={"note": "为拒绝流程恢复同一基线"},
            )
            assert rolled_back.status_code == 200, rolled_back.text

        for index, (action, expected_status) in enumerate((
            ("reject", "rejected"),
            ("request-change", "changes_requested"),
        )):
            next_workflow_id, next_handle = await start_mcad_workflow(
                tenant_id=owner.tenant_id,
                project_id=project_id,
                principal_id=owner.principal_id,
                branch_id=initial.branch_id,
                expected_base_revision_id=initial.revision_id,
                kind="generate",
                idempotency_key=f"{action}-{project_id}-{index}",
                objective=f"验证 {action} 审查动作",
                primary=_box_execution(),
            )
            assert await _wait_for_status(
                owner,
                next_workflow_id,
                {"waiting_confirmation"},
            ) == "waiting_confirmation"
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                snapshot = await client.get(
                    f"/api/tasks/{next_workflow_id}/snapshot",
                    headers=headers,
                )
                next_change_set_id = snapshot.json()["change_set"]["id"]
                reviewed = await client.post(
                    f"/api/change-sets/{next_change_set_id}/{action}",
                    headers=headers,
                    json={"note": f"真实 {action} 审查意见"},
                )
                assert reviewed.status_code == 200, reviewed.text
                assert reviewed.json()["status"] == expected_status
            cancelled = await asyncio.wait_for(next_handle.result(), timeout=60)
            assert cancelled["status"] == "cancelled"


@pytest.mark.asyncio(loop_scope="module")
async def test_real_task_cancel_api_stops_execution_without_artifacts():
    owner, project_id, initial = await _seed_project()
    temporal = await get_temporal_client()
    app = create_app()
    headers = {"Authorization": f"Bearer {OWNER_KEY}"}
    transport = httpx.ASGITransport(app=app)

    async with build_workflow_worker(temporal):
        workflow_id, handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="generate",
            idempotency_key=f"cancel-api-{project_id}",
            objective="通过任务 API 取消长时间建模",
            primary=_slow_execution(),
        )
        assert await _wait_for_status(
            owner,
            workflow_id,
            {"running"},
        ) == "running"
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            cancelled = await client.post(
                f"/api/tasks/{workflow_id}/cancel",
                headers=headers,
                json={"reason": "端到端取消验证"},
            )
            assert cancelled.status_code == 200, cancelled.text
        result = await asyncio.wait_for(handle.result(), timeout=30)
        assert result["status"] == "cancelled"

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            snapshot = await client.get(
                f"/api/tasks/{workflow_id}/snapshot",
                headers=headers,
            )
            assert snapshot.status_code == 200
            body = snapshot.json()
            assert body["status"] == "cancelled"
            assert body["artifacts"] == []
