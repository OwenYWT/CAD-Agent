"""Real PostgreSQL + MinIO Change Set review, commit, and rollback tests."""
from __future__ import annotations

import asyncio
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
from sqlalchemy import text

from app.config import settings
from app.db import close_database, get_database_engine, tenant_transaction
from app.domain.identity import user_principal
from app.domain.runs import WorkflowStatus
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.repositories.revisions import (
    compare_and_swap_branch_head,
    create_candidate_change_set,
    create_initial_branch,
)
from app.services.artifact_commit import (
    ArtifactVerificationError,
    authorize_artifact_upload,
    commit_artifacts,
)
from app.services.change_sets import (
    ArtifactEvidenceRequired,
    ChangeSetStateConflict,
    ValidationRequired,
    accept_change_set,
    commit_change_set,
    reject_change_set,
    request_change_set_modification,
    rollback_change_set,
    update_change_set_evidence,
)
from app.services.run_state import (
    create_attempt,
    create_step,
    create_workflow,
    lease_attempt,
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


async def _scenario(*, with_artifact: bool, validation_status: str):
    owner = user_principal(f"review-owner-{uuid4()}")
    project_id = uuid4()
    now = datetime.now(timezone.utc)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        await ensure_principal(connection, owner)
        await create_project(
            connection,
            project_id=project_id,
            tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id,
            name="Review project",
            slug=f"review-{project_id.hex[:8]}",
        )
        workflow = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="modify",
            idempotency_key=f"review-workflow-{project_id}",
            request_payload={"instruction": "壁厚改为 3 mm"},
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
            idempotency_key=f"review-attempt-{project_id}",
            execution_payload={"runtime": "cadquery"},
        )
        initial = await create_initial_branch(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            created_by_principal_id=owner.principal_id,
            branch_name="main",
            initial_manifest={"parameters": {"wall": 2}},
        )
        candidate = await create_candidate_change_set(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            created_by_principal_id=owner.principal_id,
            idempotency_key=f"review-candidate-{project_id}",
            objective="壁厚改为 3 mm",
            candidate_manifest={"parameters": {"wall": 3}},
            validation_summary={"status": "pending"},
            source_workflow_run_id=workflow.workflow_id,
        )
        lease = await lease_attempt(
            connection,
            attempt.attempt_id,
            worker_id="review-worker",
            now=now,
            lease_seconds=120,
        )
        await start_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
            now=now,
        )

    if with_artifact:
        payload = b"ISO-10303-21;\nCHANGE-SET\nEND-ISO-10303-21;\n"
        authorization = await authorize_artifact_upload(
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            project_id=project_id,
            revision_id=candidate.candidate_revision_id,
            attempt_id=attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
            filename="result.step",
            artifact_kind="step",
            content_type="application/step",
            declared_size_bytes=len(payload),
            declared_sha256=hashlib.sha256(payload).hexdigest(),
            now=now + timedelta(seconds=1),
        )
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.put(
                authorization.upload_url,
                content=payload,
                headers=authorization.required_headers,
            )
        assert response.status_code in {200, 204}, response.text
        await commit_artifacts(
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            attempt_id=attempt.attempt_id,
            revision_id=candidate.candidate_revision_id,
            upload_ids=[authorization.upload_id],
            lease_token=lease.token,
            lease_generation=lease.generation,
            runtime_metadata={"image_digest": "sha256:" + "a" * 64},
            now=now + timedelta(seconds=2),
        )

    await update_change_set_evidence(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        change_set_id=candidate.change_set_id,
        validation_summary={"status": validation_status, "issue_count": 0},
        risk_summary={"level": "low"},
        now=now + timedelta(seconds=3),
    )
    return {
        "owner": owner,
        "project_id": project_id,
        "workflow_id": workflow.workflow_id,
        "attempt_id": attempt.attempt_id,
        "lease": lease,
        "branch_id": initial.branch_id,
        "base_revision_id": initial.revision_id,
        "candidate_revision_id": candidate.candidate_revision_id,
        "change_set_id": candidate.change_set_id,
        "now": now,
    }


@pytest.mark.asyncio(loop_scope="module")
async def test_generation_migration_uses_original_operation_and_keeps_unknown_history_null():
    """Exercise the real 0026 DDL on old-format rows, without guessing from Head."""
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.services.cloud_documents import enqueue_operation

    proven = await _scenario(with_artifact=False, validation_status="passed")
    unknown = await _scenario(with_artifact=False, validation_status="passed")
    owner = proven['owner']
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        await enqueue_operation(conn,workflow_id=proven['workflow_id'],tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,idempotency_key='migration-original-operation',request_hash='a'*64,
            payload={'branch_id':str(proven['branch_id']),'expected_base_revision_id':str(proven['base_revision_id']),
                     'operation':'modify','objective':'Migration fixture: preserve original input generation'})
        # Real branch-head triggers advance the document generation twice.
        for revision in (proven['candidate_revision_id'],proven['base_revision_id']):
            await conn.execute(text('UPDATE project_branches SET head_revision_id=:revision WHERE id=:id'),
                {'revision':revision,'id':proven['branch_id']})
        assert await conn.scalar(text('SELECT state_version FROM cloud_documents WHERE id=:id'),{'id':proven['branch_id']}) == 2

    spec = importlib.util.spec_from_file_location('tested_generation_migration',ROOT/'alembic/versions/0026_change_set_base_generation.py')
    migration = importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    def replay_ddl(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
    # Test-only DB, same transaction; the schema remains at head after the test.
    async with get_database_engine().begin() as conn:
        await conn.run_sync(replay_ddl)
        assert await conn.scalar(text('SELECT base_state_version FROM change_sets WHERE id=:id'),{'id':proven['change_set_id']}) == 0
        assert await conn.scalar(text('SELECT base_state_version FROM change_sets WHERE id=:id'),{'id':unknown['change_set_id']}) is None
    with pytest.raises(ChangeSetStateConflict,match='原始版本代次'):
        await accept_change_set(tenant_id=unknown['owner'].tenant_id,reviewer_principal_id=unknown['owner'].principal_id,
            change_set_id=unknown['change_set_id'],review_note='Unknown old generation must not be accepted')
    with pytest.raises(Exception,match='base generation is immutable'):
        async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
            await conn.execute(text('UPDATE change_sets SET base_state_version=2 WHERE id=:id'),{'id':proven['change_set_id']})


@pytest.mark.asyncio(loop_scope="module")
async def test_accept_commit_and_rollback_are_distinct_audited_operations():
    item = await _scenario(with_artifact=True, validation_status="passed")
    accepted = await accept_change_set(
        tenant_id=item["owner"].tenant_id,
        reviewer_principal_id=item["owner"].principal_id,
        change_set_id=item["change_set_id"],
        review_note="验证通过",
        now=item["now"] + timedelta(seconds=4),
    )
    async with tenant_transaction(
        item["owner"].tenant_id,
        item["owner"].principal_id,
    ) as connection:
        head_after_accept = await connection.scalar(
            text("SELECT head_revision_id FROM project_branches WHERE id=:id"),
            {"id": item["branch_id"]},
        )
    assert accepted.status == "accepted"
    assert accepted.replayed is False
    assert head_after_accept == item["base_revision_id"]

    committed = await commit_change_set(
        tenant_id=item["owner"].tenant_id,
        reviewer_principal_id=item["owner"].principal_id,
        change_set_id=item["change_set_id"],
        now=item["now"] + timedelta(seconds=5),
    )
    assert committed.status == "committed"
    assert committed.replayed is False

    rolled_back = await rollback_change_set(
        tenant_id=item["owner"].tenant_id,
        reviewer_principal_id=item["owner"].principal_id,
        change_set_id=item["change_set_id"],
        review_note="回滚到已验证基线",
        now=item["now"] + timedelta(seconds=6),
    )
    assert rolled_back.status == "rolled_back"

    async with tenant_transaction(
        item["owner"].tenant_id,
        item["owner"].principal_id,
    ) as connection:
        stored = (
            await connection.execute(
                text(
                    "SELECT c.status, b.head_revision_id, w.status AS workflow_status "
                    "FROM change_sets c JOIN project_branches b ON b.id=c.branch_id "
                    "JOIN workflow_runs w ON w.id=c.source_workflow_run_id "
                    "WHERE c.id=:id"
                ),
                {"id": item["change_set_id"]},
            )
        ).mappings().one()
        actions = set(
            (
                await connection.execute(
                    text(
                        "SELECT action FROM audit_records "
                        "WHERE target_id=:target_id"
                    ),
                    {"target_id": str(item["change_set_id"])},
                )
            ).scalars()
        )
    assert stored == {
        "status": "rolled_back",
        "head_revision_id": item["base_revision_id"],
        "workflow_status": "succeeded",
    }
    assert {
        "change_set.accepted",
        "change_set.committed",
        "change_set.rolled_back",
    }.issubset(actions)


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("damage", ["missing", "truncated", "replaced"])
async def test_file_damage_after_accept_does_not_commit_or_lose_accepted_state(damage):
    from app.object_store import delete_object, put_object, get_object
    item = await _scenario(with_artifact=True, validation_status="passed")
    owner = item["owner"]
    args = {"tenant_id": owner.tenant_id, "reviewer_principal_id": owner.principal_id,
            "change_set_id": item["change_set_id"]}
    await accept_change_set(**args)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        key = await conn.scalar(text("SELECT object_key FROM artifacts WHERE revision_id=:id"),
                                {"id": item["candidate_revision_id"]})
    original = await get_object(key)
    try:
        if damage == "missing":
            await delete_object(key)
        else:
            data = original[:5] if damage == "truncated" else b"X" * len(original)
            await put_object(key, data, content_type="application/step")
        with pytest.raises(ArtifactEvidenceRequired):
            await commit_change_set(**args)
        async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
            assert await conn.scalar(text("SELECT status FROM change_sets WHERE id=:id"),
                                     {"id": item["change_set_id"]}) == "accepted"
            assert await conn.scalar(text("SELECT head_revision_id FROM project_branches WHERE id=:id"),
                                     {"id": item["branch_id"]}) == item["base_revision_id"]
    finally:
        await put_object(key, original, content_type="application/step")
    assert (await commit_change_set(**args)).status == "committed"


@pytest.mark.asyncio(loop_scope="module")
async def test_accept_requires_real_artifacts_and_successful_validation():
    missing_artifact = await _scenario(
        with_artifact=False,
        validation_status="passed",
    )
    expected = b"expected"
    rejected_upload = await authorize_artifact_upload(
        tenant_id=missing_artifact["owner"].tenant_id,
        principal_id=missing_artifact["owner"].principal_id,
        project_id=missing_artifact["project_id"],
        revision_id=missing_artifact["candidate_revision_id"],
        attempt_id=missing_artifact["attempt_id"],
        lease_token=missing_artifact["lease"].token,
        lease_generation=missing_artifact["lease"].generation,
        filename="rejected.step",
        artifact_kind="step",
        content_type="application/step",
        declared_size_bytes=len(expected),
        declared_sha256=hashlib.sha256(expected).hexdigest(),
        now=missing_artifact["now"] + timedelta(seconds=4),
    )
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.put(
            rejected_upload.upload_url,
            content=b"tampered",
            headers=rejected_upload.required_headers,
        )
    assert response.status_code in {200, 204}, response.text
    with pytest.raises(ArtifactVerificationError):
        await commit_artifacts(
            tenant_id=missing_artifact["owner"].tenant_id,
            principal_id=missing_artifact["owner"].principal_id,
            attempt_id=missing_artifact["attempt_id"],
            revision_id=missing_artifact["candidate_revision_id"],
            upload_ids=[rejected_upload.upload_id],
            lease_token=missing_artifact["lease"].token,
            lease_generation=missing_artifact["lease"].generation,
            now=missing_artifact["now"] + timedelta(seconds=5),
        )
    with pytest.raises(ArtifactEvidenceRequired):
        await accept_change_set(
            tenant_id=missing_artifact["owner"].tenant_id,
            reviewer_principal_id=missing_artifact["owner"].principal_id,
            change_set_id=missing_artifact["change_set_id"],
        )

    failed_validation = await _scenario(
        with_artifact=True,
        validation_status="failed",
    )
    with pytest.raises(ValidationRequired):
        await accept_change_set(
            tenant_id=failed_validation["owner"].tenant_id,
            reviewer_principal_id=failed_validation["owner"].principal_id,
            change_set_id=failed_validation["change_set_id"],
        )

    for item in (missing_artifact, failed_validation):
        async with tenant_transaction(
            item["owner"].tenant_id,
            item["owner"].principal_id,
        ) as connection:
            assert (
                await connection.scalar(
                    text("SELECT status FROM change_sets WHERE id=:id"),
                    {"id": item["change_set_id"]},
                )
            ) == "pending_review"


@pytest.mark.asyncio(loop_scope="module")
async def test_reject_and_request_change_never_advance_branch():
    requested = await _scenario(
        with_artifact=True,
        validation_status="pending",
    )
    result = await request_change_set_modification(
        tenant_id=requested["owner"].tenant_id,
        reviewer_principal_id=requested["owner"].principal_id,
        change_set_id=requested["change_set_id"],
        review_note="补充 DFM 证据",
    )
    assert result.status == "changes_requested"
    with pytest.raises(ChangeSetStateConflict):
        await accept_change_set(
            tenant_id=requested["owner"].tenant_id,
            reviewer_principal_id=requested["owner"].principal_id,
            change_set_id=requested["change_set_id"],
        )

    rejected = await _scenario(
        with_artifact=True,
        validation_status="pending",
    )
    rejection = await reject_change_set(
        tenant_id=rejected["owner"].tenant_id,
        reviewer_principal_id=rejected["owner"].principal_id,
        change_set_id=rejected["change_set_id"],
        review_note="方案不符合约束",
    )
    assert rejection.status == "rejected"

    for item in (requested, rejected):
        async with tenant_transaction(
            item["owner"].tenant_id,
            item["owner"].principal_id,
        ) as connection:
            assert (
                await connection.scalar(
                    text("SELECT head_revision_id FROM project_branches WHERE id=:id"),
                    {"id": item["branch_id"]},
                )
            ) == item["base_revision_id"]


@pytest.mark.asyncio(loop_scope="module")
async def test_concurrent_accept_and_commit_are_idempotent_and_cas_safe():
    item = await _scenario(with_artifact=True, validation_status="passed")

    async def accept():
        return await accept_change_set(
            tenant_id=item["owner"].tenant_id,
            reviewer_principal_id=item["owner"].principal_id,
            change_set_id=item["change_set_id"],
        )

    accepted = await asyncio.gather(accept(), accept())
    assert sorted(result.replayed for result in accepted) == [False, True]

    async def commit():
        return await commit_change_set(
            tenant_id=item["owner"].tenant_id,
            reviewer_principal_id=item["owner"].principal_id,
            change_set_id=item["change_set_id"],
        )

    committed = await asyncio.gather(commit(), commit())
    assert sorted(result.replayed for result in committed) == [False, True]
    async with tenant_transaction(
        item["owner"].tenant_id,
        item["owner"].principal_id,
    ) as connection:
        stored = (
            await connection.execute(
                text(
                    "SELECT c.status, b.head_revision_id "
                    "FROM change_sets c JOIN project_branches b ON b.id=c.branch_id "
                    "WHERE c.id=:id"
                ),
                {"id": item["change_set_id"]},
            )
        ).mappings().one()
    assert stored == {
        "status": "committed",
        "head_revision_id": item["candidate_revision_id"],
    }


@pytest.mark.asyncio(loop_scope="module")
async def test_stale_base_cannot_commit_an_already_accepted_change_set():
    item = await _scenario(with_artifact=True, validation_status="passed")
    await accept_change_set(
        tenant_id=item["owner"].tenant_id,
        reviewer_principal_id=item["owner"].principal_id,
        change_set_id=item["change_set_id"],
    )
    async with tenant_transaction(
        item["owner"].tenant_id,
        item["owner"].principal_id,
    ) as connection:
        sibling = await create_candidate_change_set(
            connection,
            tenant_id=item["owner"].tenant_id,
            project_id=item["project_id"],
            branch_id=item["branch_id"],
            expected_base_revision_id=item["base_revision_id"],
            created_by_principal_id=item["owner"].principal_id,
            idempotency_key=f"sibling-{item['project_id']}",
            objective="并行候选",
            candidate_manifest={"parameters": {"wall": 4}},
        )
        assert await compare_and_swap_branch_head(
            connection,
            tenant_id=item["owner"].tenant_id,
            project_id=item["project_id"],
            branch_id=item["branch_id"],
            expected_head_revision_id=item["base_revision_id"],
            candidate_revision_id=sibling.candidate_revision_id,
        )

    with pytest.raises(ChangeSetStateConflict, match="stale"):
        await commit_change_set(
            tenant_id=item["owner"].tenant_id,
            reviewer_principal_id=item["owner"].principal_id,
            change_set_id=item["change_set_id"],
        )
    async with tenant_transaction(
        item["owner"].tenant_id,
        item["owner"].principal_id,
    ) as connection:
        assert (
            await connection.scalar(
                text("SELECT status FROM change_sets WHERE id=:id"),
                {"id": item["change_set_id"]},
            )
        ) == "accepted"
