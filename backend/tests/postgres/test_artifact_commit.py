"""Real PostgreSQL + MinIO two-phase immutable artifact tests."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import text

from app.config import settings
from app.db import close_database, get_database_engine, tenant_transaction
from app.domain.identity import user_principal
from app.domain.runs import WorkflowStatus
from app.object_store import (
    get_object,
    head_object,
    put_object,
    reset_object_store_client,
)
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.repositories.revisions import (
    create_candidate_change_set,
    create_initial_branch,
)
from app.services.artifact_commit import (
    ArtifactVerificationError,
    authorize_artifact_upload,
    cleanup_artifact_orphans,
    commit_artifacts,
)
from app.services.run_state import (
    IllegalTransition,
    StaleLease,
    create_attempt,
    create_step,
    create_workflow,
    lease_attempt,
    request_workflow_cancellation,
    start_attempt,
    transition_workflow,
)


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
RUN_MINIO = os.environ.get("CAD_AGENT_TEST_OBJECT_STORE") == "1"
ORIGINAL_DATABASE_URL = settings.database_url
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not RUN_MINIO,
    reason="real PostgreSQL and MinIO test configuration is required",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    if not TEST_DATABASE_URL or not RUN_MINIO:
        yield
        return
    settings.database_url = TEST_DATABASE_URL
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def database_engine_lifecycle(migrated_database):
    settings.database_url = TEST_DATABASE_URL
    yield
    await close_database()
    settings.database_url = ORIGINAL_DATABASE_URL


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean_control_plane():
    if not TEST_DATABASE_URL or not RUN_MINIO:
        yield
        return
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
    yield


async def _seed_active_attempt(*, lease_seconds: int = 60):
    owner = user_principal(f"artifact-owner-{uuid4()}")
    project_id = uuid4()
    now = datetime.now(timezone.utc)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        await ensure_principal(connection, owner)
        await create_project(
            connection,
            project_id=project_id,
            tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id,
            name="Artifact project",
            slug=f"artifact-{project_id.hex[:8]}",
        )
        workflow = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="generate",
            idempotency_key=f"artifact-workflow-{project_id}",
            request_payload={"prompt": "生成工装夹具"},
        )
        await transition_workflow(
            connection,
            workflow.workflow_id,
            expected=WorkflowStatus.PENDING,
            target=WorkflowStatus.PLANNING,
            now=now,
        )
        await transition_workflow(
            connection,
            workflow.workflow_id,
            expected=WorkflowStatus.PLANNING,
            target=WorkflowStatus.RUNNING,
            now=now,
        )
        step = await create_step(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_key="model",
            step_index=1,
            kind="mcad_model",
        )
        attempt = await create_attempt(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_id=step.step_id,
            idempotency_key=f"artifact-attempt-{project_id}",
            execution_payload={"runtime": "cadquery"},
        )
        initial = await create_initial_branch(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            created_by_principal_id=owner.principal_id,
            branch_name="main",
            initial_manifest={"state": "empty"},
        )
        candidate = await create_candidate_change_set(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            created_by_principal_id=owner.principal_id,
            idempotency_key=f"artifact-candidate-{project_id}",
            objective="生成首个可制造模型",
            candidate_manifest={"state": "generated"},
            source_workflow_run_id=workflow.workflow_id,
        )
        lease = await lease_attempt(
            connection,
            attempt.attempt_id,
            worker_id="podman-worker",
            now=now,
            lease_seconds=lease_seconds,
        )
        await start_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
            now=now,
        )
    return {
        "owner": owner,
        "project_id": project_id,
        "workflow_id": workflow.workflow_id,
        "attempt_id": attempt.attempt_id,
        "revision_id": candidate.candidate_revision_id,
        "lease": lease,
        "now": now,
    }


async def _authorize_and_upload(
    context,
    *,
    filename: str,
    artifact_kind: str,
    payload: bytes,
    declared_payload: bytes | None = None,
):
    declared = declared_payload if declared_payload is not None else payload
    authorization = await authorize_artifact_upload(
        tenant_id=context["owner"].tenant_id,
        principal_id=context["owner"].principal_id,
        project_id=context["project_id"],
        revision_id=context["revision_id"],
        attempt_id=context["attempt_id"],
        lease_token=context["lease"].token,
        lease_generation=context["lease"].generation,
        filename=filename,
        artifact_kind=artifact_kind,
        content_type="application/octet-stream",
        declared_size_bytes=len(declared),
        declared_sha256=hashlib.sha256(declared).hexdigest(),
        now=context["now"] + timedelta(seconds=1),
    )
    assert authorization.upload_url not in repr(authorization)
    assert settings.object_store_secret_key not in authorization.upload_url
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.put(
            authorization.upload_url,
            content=payload,
            headers=authorization.required_headers,
        )
    assert response.status_code in {200, 204}, response.text
    return authorization


@pytest.mark.asyncio(loop_scope="module")
async def test_two_phase_commit_creates_immutable_artifacts_and_replays_once():
    context = await _seed_active_attempt()
    step_bytes = b"ISO-10303-21;\nSTEP-CONTENT\nEND-ISO-10303-21;\n"
    stl_bytes = b"solid fixture\nendsolid fixture\n"
    step = await _authorize_and_upload(
        context,
        filename="result.step",
        artifact_kind="step",
        payload=step_bytes,
    )
    stl = await _authorize_and_upload(
        context,
        filename="result.stl",
        artifact_kind="stl",
        payload=stl_bytes,
    )

    committed = await commit_artifacts(
        tenant_id=context["owner"].tenant_id,
        principal_id=context["owner"].principal_id,
        attempt_id=context["attempt_id"],
        revision_id=context["revision_id"],
        upload_ids=[step.upload_id, stl.upload_id],
        lease_token=context["lease"].token,
        lease_generation=context["lease"].generation,
        now=context["now"] + timedelta(seconds=2),
    )
    replay = await commit_artifacts(
        tenant_id=context["owner"].tenant_id,
        principal_id=context["owner"].principal_id,
        attempt_id=context["attempt_id"],
        revision_id=context["revision_id"],
        upload_ids=[step.upload_id, stl.upload_id],
        lease_token=context["lease"].token,
        lease_generation=context["lease"].generation,
        now=context["now"] + timedelta(seconds=3),
    )

    assert committed.replayed is False
    assert replay.replayed is True
    assert {artifact.filename for artifact in committed.artifacts} == {
        "result.step",
        "result.stl",
    }
    async with tenant_transaction(
        context["owner"].tenant_id,
        context["owner"].principal_id,
    ) as connection:
        counts = (
            await connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM artifacts WHERE attempt_id=:attempt_id) "
                    "AS artifacts, "
                    "(SELECT count(*) FROM task_events "
                    " WHERE workflow_run_id=:workflow_id "
                    " AND event_type='artifact.committed') AS artifact_events, "
                    "(SELECT status FROM execution_attempts WHERE id=:attempt_id) "
                    "AS attempt_status"
                ),
                {
                    "attempt_id": context["attempt_id"],
                    "workflow_id": context["workflow_id"],
                },
            )
        ).mappings().one()
    assert counts == {
        "artifacts": 2,
        "artifact_events": 1,
        "attempt_status": "succeeded",
    }
    async with get_database_engine().connect() as connection:
        rls_rows = (
            await connection.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class "
                    "WHERE relname = ANY(:table_names)"
                ),
                {"table_names": ["artifact_uploads", "artifacts"]},
            )
        ).mappings().all()
    assert {row["relname"] for row in rls_rows} == {
        "artifact_uploads",
        "artifacts",
    }
    assert all(row["relrowsecurity"] for row in rls_rows)
    assert all(row["relforcerowsecurity"] for row in rls_rows)

    with pytest.raises(Exception, match="immutable|permission denied"):
        async with tenant_transaction(
            context["owner"].tenant_id,
            context["owner"].principal_id,
        ) as connection:
            await connection.execute(
                text("UPDATE artifacts SET filename='tampered.step'"),
            )
    for artifact in committed.artifacts:
        assert "current." not in artifact.object_key
        assert str(context["revision_id"]) in artifact.object_key
        metadata = await head_object(artifact.object_key)
        assert metadata["size_bytes"] == artifact.size_bytes
        assert hashlib.sha256(await get_object(artifact.object_key)).hexdigest() == (
            artifact.sha256
        )


@pytest.mark.asyncio(loop_scope="module")
async def test_partial_upload_and_hash_mismatch_cannot_commit_success():
    context = await _seed_active_attempt()
    present = await _authorize_and_upload(
        context,
        filename="present.step",
        artifact_kind="step",
        payload=b"present",
    )
    missing = await authorize_artifact_upload(
        tenant_id=context["owner"].tenant_id,
        principal_id=context["owner"].principal_id,
        project_id=context["project_id"],
        revision_id=context["revision_id"],
        attempt_id=context["attempt_id"],
        lease_token=context["lease"].token,
        lease_generation=context["lease"].generation,
        filename="missing.stl",
        artifact_kind="stl",
        content_type="application/octet-stream",
        declared_size_bytes=7,
        declared_sha256=hashlib.sha256(b"missing").hexdigest(),
        now=context["now"] + timedelta(seconds=1),
    )
    with pytest.raises(ArtifactVerificationError):
        await commit_artifacts(
            tenant_id=context["owner"].tenant_id,
            principal_id=context["owner"].principal_id,
            attempt_id=context["attempt_id"],
            revision_id=context["revision_id"],
            upload_ids=[present.upload_id, missing.upload_id],
            lease_token=context["lease"].token,
            lease_generation=context["lease"].generation,
            now=context["now"] + timedelta(seconds=2),
        )

    mismatch_context = await _seed_active_attempt()
    mismatched = await _authorize_and_upload(
        mismatch_context,
        filename="mismatch.step",
        artifact_kind="step",
        payload=b"actual!",
        declared_payload=b"expect!",
    )
    with pytest.raises(ArtifactVerificationError, match="SHA-256"):
        await commit_artifacts(
            tenant_id=mismatch_context["owner"].tenant_id,
            principal_id=mismatch_context["owner"].principal_id,
            attempt_id=mismatch_context["attempt_id"],
            revision_id=mismatch_context["revision_id"],
            upload_ids=[mismatched.upload_id],
            lease_token=mismatch_context["lease"].token,
            lease_generation=mismatch_context["lease"].generation,
            now=mismatch_context["now"] + timedelta(seconds=2),
        )

    for item in (context, mismatch_context):
        async with tenant_transaction(
            item["owner"].tenant_id,
            item["owner"].principal_id,
        ) as connection:
            artifacts = await connection.scalar(
                text("SELECT count(*) FROM artifacts WHERE attempt_id=:id"),
                {"id": item["attempt_id"]},
            )
            attempt_status = await connection.scalar(
                text("SELECT status FROM execution_attempts WHERE id=:id"),
                {"id": item["attempt_id"]},
            )
            upload_statuses = set(
                (
                    await connection.execute(
                        text(
                            "SELECT status FROM artifact_uploads "
                            "WHERE attempt_id=:id"
                        ),
                        {"id": item["attempt_id"]},
                    )
                ).scalars()
            )
        assert artifacts == 0
        assert attempt_status == "running"
        assert upload_statuses == {"rejected"}


@pytest.mark.asyncio(loop_scope="module")
async def test_stale_lease_and_cancellation_cannot_attach_uploaded_bytes():
    stale = await _seed_active_attempt(lease_seconds=2)
    upload = await _authorize_and_upload(
        stale,
        filename="stale.step",
        artifact_kind="step",
        payload=b"stale",
    )
    async with tenant_transaction(
        stale["owner"].tenant_id,
        stale["owner"].principal_id,
    ) as connection:
        replacement = await lease_attempt(
            connection,
            stale["attempt_id"],
            worker_id="replacement-worker",
            now=stale["now"] + timedelta(seconds=3),
            lease_seconds=30,
        )
    with pytest.raises(StaleLease):
        await commit_artifacts(
            tenant_id=stale["owner"].tenant_id,
            principal_id=stale["owner"].principal_id,
            attempt_id=stale["attempt_id"],
            revision_id=stale["revision_id"],
            upload_ids=[upload.upload_id],
            lease_token=stale["lease"].token,
            lease_generation=stale["lease"].generation,
            now=stale["now"] + timedelta(seconds=4),
        )
    assert replacement.generation == stale["lease"].generation + 1

    cancelled = await _seed_active_attempt()
    cancelled_upload = await _authorize_and_upload(
        cancelled,
        filename="cancelled.step",
        artifact_kind="step",
        payload=b"cancelled",
    )
    async with tenant_transaction(
        cancelled["owner"].tenant_id,
        cancelled["owner"].principal_id,
    ) as connection:
        await request_workflow_cancellation(
            connection,
            cancelled["workflow_id"],
            now=cancelled["now"] + timedelta(seconds=2),
        )
    with pytest.raises(IllegalTransition, match="cancellation"):
        await commit_artifacts(
            tenant_id=cancelled["owner"].tenant_id,
            principal_id=cancelled["owner"].principal_id,
            attempt_id=cancelled["attempt_id"],
            revision_id=cancelled["revision_id"],
            upload_ids=[cancelled_upload.upload_id],
            lease_token=cancelled["lease"].token,
            lease_generation=cancelled["lease"].generation,
            now=cancelled["now"] + timedelta(seconds=3),
        )

    for item in (stale, cancelled):
        async with tenant_transaction(
            item["owner"].tenant_id,
            item["owner"].principal_id,
        ) as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM artifacts WHERE attempt_id=:id"),
                    {"id": item["attempt_id"]},
                )
            ) == 0


@pytest.mark.asyncio(loop_scope="module")
async def test_wrong_revision_and_real_object_store_outage_fail_closed(
    monkeypatch,
):
    context = await _seed_active_attempt()
    with pytest.raises(IllegalTransition, match="revision"):
        await authorize_artifact_upload(
            tenant_id=context["owner"].tenant_id,
            principal_id=context["owner"].principal_id,
            project_id=context["project_id"],
            revision_id=uuid4(),
            attempt_id=context["attempt_id"],
            lease_token=context["lease"].token,
            lease_generation=context["lease"].generation,
            filename="wrong.step",
            artifact_kind="step",
            content_type="application/step",
            declared_size_bytes=5,
            declared_sha256=hashlib.sha256(b"wrong").hexdigest(),
            now=context["now"] + timedelta(seconds=1),
        )

    upload = await _authorize_and_upload(
        context,
        filename="outage.step",
        artifact_kind="step",
        payload=b"outage",
    )
    original_endpoint = settings.object_store_endpoint_url
    monkeypatch.setattr(
        settings,
        "object_store_endpoint_url",
        "http://127.0.0.1:1",
    )
    reset_object_store_client()
    try:
        with pytest.raises(EndpointConnectionError):
            await commit_artifacts(
                tenant_id=context["owner"].tenant_id,
                principal_id=context["owner"].principal_id,
                attempt_id=context["attempt_id"],
                revision_id=context["revision_id"],
                upload_ids=[upload.upload_id],
                lease_token=context["lease"].token,
                lease_generation=context["lease"].generation,
                now=context["now"] + timedelta(seconds=2),
            )
    finally:
        monkeypatch.setattr(
            settings,
            "object_store_endpoint_url",
            original_endpoint,
        )
        reset_object_store_client()

    async with tenant_transaction(
        context["owner"].tenant_id,
        context["owner"].principal_id,
    ) as connection:
        stored = (
            await connection.execute(
                text(
                    "SELECT u.status AS upload_status, a.status AS attempt_status, "
                    "(SELECT count(*) FROM artifacts WHERE attempt_id=a.id) "
                    "AS artifact_count "
                    "FROM artifact_uploads u JOIN execution_attempts a "
                    "ON a.id=u.attempt_id WHERE u.id=:upload_id"
                ),
                {"upload_id": upload.upload_id},
            )
        ).mappings().one()
    assert stored == {
        "upload_status": "authorized",
        "attempt_status": "running",
        "artifact_count": 0,
    }


@pytest.mark.asyncio(loop_scope="module")
async def test_orphan_cleanup_deletes_unknown_objects_but_preserves_artifacts():
    context = await _seed_active_attempt()
    upload = await _authorize_and_upload(
        context,
        filename="preserved.step",
        artifact_kind="step",
        payload=b"preserved",
    )
    committed = await commit_artifacts(
        tenant_id=context["owner"].tenant_id,
        principal_id=context["owner"].principal_id,
        attempt_id=context["attempt_id"],
        revision_id=context["revision_id"],
        upload_ids=[upload.upload_id],
        lease_token=context["lease"].token,
        lease_generation=context["lease"].generation,
        now=context["now"] + timedelta(seconds=2),
    )
    preserved_key = committed.artifacts[0].object_key
    staging_orphan = (
        f"staging/tenants/{context['owner'].tenant_id}/"
        f"unregistered/{uuid4()}.step"
    )
    final_orphan = (
        f"tenants/{context['owner'].tenant_id}/projects/"
        f"{context['project_id']}/orphan/{uuid4()}.step"
    )
    await put_object(
        staging_orphan,
        b"staging-orphan",
        content_type="application/step",
    )
    await put_object(
        final_orphan,
        b"final-orphan",
        content_type="application/step",
    )

    report = await cleanup_artifact_orphans(
        tenant_id=context["owner"].tenant_id,
        principal_id=context["owner"].principal_id,
        now=datetime.now(timezone.utc) + timedelta(seconds=1),
        grace_seconds=0,
    )

    assert report["deleted_staging_objects"] >= 1
    assert report["deleted_final_orphans"] == 1
    assert await get_object(preserved_key) == b"preserved"
    for orphan_key in (staging_orphan, final_orphan):
        with pytest.raises(ClientError):
            await head_object(orphan_key)
