"""Real PostgreSQL candidate-build and staging-evidence contracts."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import settings
from app.db import close_database, get_database_engine, tenant_transaction
from app.domain.identity import user_principal
from app.domain.revisions import CandidateBuildStatus
from app.repositories.agent_candidates import (
    CandidateBuildConflict,
    StaleExecutionAttempt,
    ValidationEvidenceConflict,
    accept_staging_manifest,
    create_agent_candidate_build,
    create_candidate_seal,
    record_validation_evidence,
    transition_agent_candidate_build,
)
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.repositories.revisions import create_initial_branch
from app.services.run_state import (
    complete_attempt,
    create_attempt,
    create_step,
    create_workflow,
    lease_attempt,
    start_attempt,
    transition_step,
    transition_workflow,
)
from app.domain.runs import StepStatus, WorkflowStatus


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
ORIGINAL_DATABASE_URL = settings.database_url
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="CAD_AGENT_TEST_DATABASE_URL is required for real PostgreSQL tests",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    if not TEST_DATABASE_URL:
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
    if not TEST_DATABASE_URL:
        yield
        return
    async with get_database_engine().begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE agent_candidate_seals, agent_validation_evidence, "
                "agent_staging_manifests, agent_candidate_builds, artifacts, "
                "artifact_uploads, change_sets, project_revisions, "
                "project_branches, outbox_messages, task_events, "
                "execution_attempts, step_runs, workflow_runs, "
                "usage_meter_entries, audit_records, project_memberships, "
                "projects, tenant_memberships, principals, tenants CASCADE"
            )
        )
    yield


async def _seed():
    owner = user_principal(f"candidate-owner-{uuid4()}")
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
            name="Candidate Build",
            slug=f"candidate-{project_id.hex[:8]}",
        )
        initial = await create_initial_branch(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            created_by_principal_id=owner.principal_id,
            branch_name="main",
            initial_manifest={"state": "empty"},
        )
        workflow = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"candidate-{uuid4()}",
            request_payload={"objective": "创建支架"},
        )
        await transition_workflow(
            connection,
            workflow.workflow_id,
            expected=WorkflowStatus.PENDING,
            target=WorkflowStatus.PLANNING,
        )
        await transition_workflow(
            connection,
            workflow.workflow_id,
            expected=WorkflowStatus.PLANNING,
            target=WorkflowStatus.RUNNING,
        )
        candidate = await create_agent_candidate_build(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            branch_id=initial.branch_id,
            base_revision_id=initial.revision_id,
            workflow_id=workflow.workflow_id,
            created_by_principal_id=owner.principal_id,
            plan={"schema_version": "durable-agent-plan.v1", "objective": "创建支架"},
        )
        step = await create_step(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_key="model-main",
            step_index=3,
            kind="agent_model",
        )
        await transition_step(
            connection,
            step.step_id,
            expected=StepStatus.PENDING,
            target=StepStatus.READY,
        )
        await transition_step(
            connection,
            step.step_id,
            expected=StepStatus.READY,
            target=StepStatus.RUNNING,
        )
        attempt = await create_attempt(
            connection,
            tenant_id=owner.tenant_id,
            workflow_id=workflow.workflow_id,
            step_id=step.step_id,
            idempotency_key=f"attempt-{uuid4()}",
            execution_payload={"operation": "model"},
        )
        lease = await lease_attempt(
            connection,
            attempt.attempt_id,
            worker_id="candidate-test",
            lease_seconds=60,
        )
        await start_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
        )
    return owner, project_id, initial, workflow, candidate, step, attempt, lease


async def _succeed_attempt(owner, attempt, step, lease):
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        await complete_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
            result_payload={"status": "succeeded"},
        )
        await transition_step(
            connection,
            step.step_id,
            expected=StepStatus.RUNNING,
            target=StepStatus.SUCCEEDED,
        )


@pytest.mark.asyncio(loop_scope="module")
async def test_candidate_create_and_terminal_lifecycle_are_idempotent():
    owner, project_id, initial, workflow, candidate, *_ = await _seed()
    plan = {"schema_version": "durable-agent-plan.v1", "objective": "创建支架"}
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        replay = await create_agent_candidate_build(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            branch_id=initial.branch_id,
            base_revision_id=initial.revision_id,
            workflow_id=workflow.workflow_id,
            created_by_principal_id=owner.principal_id,
            plan=plan,
        )
        with pytest.raises(CandidateBuildConflict):
            await create_agent_candidate_build(
                connection,
                tenant_id=owner.tenant_id,
                project_id=project_id,
                branch_id=initial.branch_id,
                base_revision_id=initial.revision_id,
                workflow_id=workflow.workflow_id,
                created_by_principal_id=owner.principal_id,
                plan={**plan, "objective": "不同目标"},
            )
        failed = await transition_agent_candidate_build(
            connection,
            tenant_id=owner.tenant_id,
            candidate_build_id=candidate.candidate_build_id,
            expected=CandidateBuildStatus.BUILDING,
            target=CandidateBuildStatus.FAILED,
            failure_code="model_failed",
            failure_message="kernel error",
        )
        replayed_failure = await transition_agent_candidate_build(
            connection,
            tenant_id=owner.tenant_id,
            candidate_build_id=candidate.candidate_build_id,
            expected=CandidateBuildStatus.BUILDING,
            target=CandidateBuildStatus.FAILED,
            failure_code="model_failed",
            failure_message="kernel error",
        )
    assert replay.replayed is True
    assert replay.candidate_build_id == candidate.candidate_build_id
    assert failed.status is CandidateBuildStatus.FAILED
    assert replayed_failure.replayed is True


@pytest.mark.asyncio(loop_scope="module")
async def test_staging_manifest_requires_current_succeeded_lease_and_replays():
    owner, _, _, workflow, candidate, step, attempt, lease = await _seed()
    manifest = {
        "schema_version": "agent-staging-manifest.v1",
        "source_sha256": "a" * 64,
        "outputs": [
            {
                "name": "step",
                "object_key": "staging/output.step",
                "sha256": "b" * 64,
                "size_bytes": 123,
            }
        ],
    }
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        with pytest.raises(StaleExecutionAttempt):
            await accept_staging_manifest(
                connection,
                tenant_id=owner.tenant_id,
                candidate_build_id=candidate.candidate_build_id,
                workflow_id=workflow.workflow_id,
                step_id=step.step_id,
                attempt_id=attempt.attempt_id,
                lease_generation=lease.generation,
                lease_token=lease.token,
                manifest=manifest,
            )
    await _succeed_attempt(owner, attempt, step, lease)
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        with pytest.raises(StaleExecutionAttempt):
            await accept_staging_manifest(
                connection,
                tenant_id=owner.tenant_id,
                candidate_build_id=candidate.candidate_build_id,
                workflow_id=workflow.workflow_id,
                step_id=step.step_id,
                attempt_id=attempt.attempt_id,
                lease_generation=lease.generation,
                lease_token="stale-token",
                manifest=manifest,
            )
        accepted = await accept_staging_manifest(
            connection,
            tenant_id=owner.tenant_id,
            candidate_build_id=candidate.candidate_build_id,
            workflow_id=workflow.workflow_id,
            step_id=step.step_id,
            attempt_id=attempt.attempt_id,
            lease_generation=lease.generation,
            lease_token=lease.token,
            manifest=manifest,
        )
        replayed = await accept_staging_manifest(
            connection,
            tenant_id=owner.tenant_id,
            candidate_build_id=candidate.candidate_build_id,
            workflow_id=workflow.workflow_id,
            step_id=step.step_id,
            attempt_id=attempt.attempt_id,
            lease_generation=lease.generation,
            lease_token=lease.token,
            manifest=manifest,
        )
        with pytest.raises(CandidateBuildConflict):
            await accept_staging_manifest(
                connection,
                tenant_id=owner.tenant_id,
                candidate_build_id=candidate.candidate_build_id,
                workflow_id=workflow.workflow_id,
                step_id=step.step_id,
                attempt_id=attempt.attempt_id,
                lease_generation=lease.generation,
                lease_token=lease.token,
                manifest={**manifest, "source_sha256": "c" * 64},
            )
    assert replayed.staging_manifest_id == accepted.staging_manifest_id
    assert replayed.replayed is True


@pytest.mark.asyncio(loop_scope="module")
async def test_evidence_is_candidate_scoped_immutable_and_idempotent():
    owner, _, _, workflow, candidate, step, attempt, lease = await _seed()
    await _succeed_attempt(owner, attempt, step, lease)
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        manifest = await accept_staging_manifest(
            connection,
            tenant_id=owner.tenant_id,
            candidate_build_id=candidate.candidate_build_id,
            workflow_id=workflow.workflow_id,
            step_id=step.step_id,
            attempt_id=attempt.attempt_id,
            lease_generation=lease.generation,
            lease_token=lease.token,
            manifest={"schema_version": "agent-staging-manifest.v1", "outputs": []},
        )
        evidence = await record_validation_evidence(
            connection,
            tenant_id=owner.tenant_id,
            candidate_build_id=candidate.candidate_build_id,
            workflow_id=workflow.workflow_id,
            step_id=step.step_id,
            attempt_id=attempt.attempt_id,
            staging_manifest_id=manifest.staging_manifest_id,
            gate="artifact_integrity",
            mode="required",
            outcome="passed",
            evidence={"verified": True},
        )
        replay = await record_validation_evidence(
            connection,
            tenant_id=owner.tenant_id,
            candidate_build_id=candidate.candidate_build_id,
            workflow_id=workflow.workflow_id,
            step_id=step.step_id,
            attempt_id=attempt.attempt_id,
            staging_manifest_id=manifest.staging_manifest_id,
            gate="artifact_integrity",
            mode="required",
            outcome="passed",
            evidence={"verified": True},
        )
        with pytest.raises(ValidationEvidenceConflict):
            await record_validation_evidence(
                connection,
                tenant_id=owner.tenant_id,
                candidate_build_id=uuid4(),
                workflow_id=workflow.workflow_id,
                step_id=step.step_id,
                staging_manifest_id=manifest.staging_manifest_id,
                gate="geometry",
                mode="required",
                outcome="passed",
                evidence={"solid": True},
            )
        with pytest.raises(Exception, match="permission denied|immutable"):
            await connection.execute(
                text(
                    "UPDATE agent_validation_evidence "
                    "SET outcome='failed' WHERE id=:id"
                ),
                {"id": evidence.evidence_id},
            )
    assert replay.evidence_id == evidence.evidence_id
    assert replay.replayed is True


@pytest.mark.asyncio(loop_scope="module")
async def test_seal_identity_is_one_per_candidate_and_content_bound():
    owner, *_rest = await _seed()
    candidate = _rest[3]
    selection = {"manifest_ids": [str(uuid4())]}
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        created = await create_candidate_seal(
            connection,
            tenant_id=owner.tenant_id,
            candidate_build_id=candidate.candidate_build_id,
            seal_key="seal-once",
            selection=selection,
        )
        replay = await create_candidate_seal(
            connection,
            tenant_id=owner.tenant_id,
            candidate_build_id=candidate.candidate_build_id,
            seal_key="seal-once",
            selection=selection,
        )
        with pytest.raises(CandidateBuildConflict):
            await create_candidate_seal(
                connection,
                tenant_id=owner.tenant_id,
                candidate_build_id=candidate.candidate_build_id,
                seal_key="seal-once",
                selection={"manifest_ids": [str(uuid4())]},
            )
        with pytest.raises(CandidateBuildConflict):
            await create_candidate_seal(
                connection,
                tenant_id=owner.tenant_id,
                candidate_build_id=candidate.candidate_build_id,
                seal_key="another-seal",
                selection=selection,
            )
    assert replay.seal_id == created.seal_id
    assert replay.replayed is True


@pytest.mark.asyncio(loop_scope="module")
async def test_agent_candidate_tables_force_tenant_rls_and_worker_permissions():
    expected_tables = {
        "agent_candidate_builds",
        "agent_staging_manifests",
        "agent_validation_evidence",
        "agent_candidate_seals",
    }
    async with get_database_engine().connect() as connection:
        rls = (
            await connection.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:tables)"
                ),
                {"tables": list(expected_tables)},
            )
        ).mappings().all()
        privileges = {
            table: {
                permission: bool(
                    await connection.scalar(
                        text(
                            "SELECT has_table_privilege("
                            "'cad_agent_worker', :table, :permission)"
                        ),
                        {"table": table, "permission": permission},
                    )
                )
                for permission in ("SELECT", "INSERT", "UPDATE", "DELETE")
            }
            for table in expected_tables
        }
    assert {row["relname"] for row in rls} == expected_tables
    assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in rls)
    assert privileges["agent_staging_manifests"] == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": False,
        "DELETE": False,
    }
    assert privileges["agent_validation_evidence"] == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": False,
        "DELETE": False,
    }
