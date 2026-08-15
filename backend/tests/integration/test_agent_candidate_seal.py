"""Real PostgreSQL + MinIO candidate seal and crash-reconciliation tests."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.agent.durable_plan import AgentPlan
from app.config import settings
from app.db import close_database, get_database_engine, tenant_transaction
from app.domain.identity import user_principal
from app.domain.runs import StepStatus, WorkflowStatus
from app.object_store import delete_object, get_object, put_object
from app.repositories.agent_candidates import (
    accept_staging_manifest,
    create_agent_candidate_build,
    record_generated_source,
    record_validation_evidence,
)
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.repositories.revisions import (
    StaleBaseRevision,
    compare_and_swap_branch_head,
    create_candidate_change_set,
    create_initial_branch,
)
from app.services.artifact_commit import (
    CandidateSealVerificationError,
    cleanup_artifact_orphans,
    seal_agent_candidate,
)
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
from app.models.schemas import DesignBrief


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
RUN_MINIO = os.environ.get("CAD_AGENT_TEST_OBJECT_STORE") == "1"
ORIGINAL_DATABASE_URL = settings.database_url
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not RUN_MINIO,
    reason="real PostgreSQL and MinIO are required",
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
        object_keys = list(
            (
                await connection.execute(
                    text(
                        "SELECT object_key FROM artifacts UNION "
                        "SELECT staging_object_key FROM artifact_uploads"
                    )
                )
            ).scalars()
        )
    for key in object_keys:
        await delete_object(key)
    async with get_database_engine().begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE agent_seal_evidence, agent_seal_manifests, "
                "agent_candidate_seals, agent_validation_evidence, "
                "agent_staging_manifests, agent_candidate_builds, artifacts, "
                "artifact_uploads, change_sets, project_revisions, "
                "project_branches, outbox_messages, task_events, "
                "execution_attempts, step_runs, workflow_runs, "
                "usage_meter_entries, audit_records, project_memberships, "
                "projects, tenant_memberships, principals, tenants CASCADE"
            )
        )
    yield


def _plan(objective: str = "创建支架") -> AgentPlan:
    return AgentPlan.model_validate(
        {
            "objective": objective,
            "operation": "generate",
            "model_kind": "simple",
            "modeling_strategy": "parametric",
            "design_brief": DesignBrief(
                intent_summary=objective,
                artifact_type="bracket",
            ).model_dump(mode="json"),
            "affected_objects": [
                {
                    "object_id": "part-main",
                    "object_type": "part",
                    "label": "支架",
                    "change": "create",
                }
            ],
            "steps": [
                {
                    "step_key": "model-main",
                    "kind": "model",
                    "description": objective,
                    "affected_object_ids": ["part-main"],
                    "output_formats": ["step", "stl"],
                }
            ],
        }
    )


async def _seed_selection(*, two_outputs: bool = True):
    owner = user_principal(f"seal-owner-{uuid4()}")
    project_id = uuid4()
    plan = _plan()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        await ensure_principal(connection, owner)
        await create_project(
            connection,
            project_id=project_id,
            tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id,
            name="Seal candidate",
            slug=f"seal-{project_id.hex[:8]}",
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
            idempotency_key=f"seal-{project_id}",
            request_payload={"objective": plan.objective},
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
            plan=plan.temporal_payload(),
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
            idempotency_key=f"seal-attempt-{project_id}",
            execution_payload={"operation": "model"},
        )
        lease = await lease_attempt(
            connection,
            attempt.attempt_id,
            worker_id="seal-test",
            lease_seconds=60,
        )
        await start_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
        )
        source_code = "result = make_box(20, 10, 4)\n"
        source = await record_generated_source(
            connection,
            tenant_id=owner.tenant_id,
            candidate_build_id=candidate.candidate_build_id,
            workflow_id=workflow.workflow_id,
            step_id=step.step_id,
            source_code=source_code,
            generator_kind="controlled",
            provider="controlled",
            model="controlled",
            request_hash="a" * 64,
            response_hash="b" * 64,
        )
        outputs = []
        for index, suffix in enumerate(("step", "stl") if two_outputs else ("step",)):
            payload = f"sealed-{suffix}-{project_id}".encode()
            digest = hashlib.sha256(payload).hexdigest()
            key = (
                f"staging/agent/tenants/{owner.tenant_id}/candidates/"
                f"{candidate.candidate_build_id}/attempts/{attempt.attempt_id}/"
                f"{digest}/geometry-{index:02d}.{suffix}"
            )
            await put_object(key, payload, content_type="application/octet-stream")
            outputs.append(
                {
                    "format": suffix,
                    "filename": f"geometry-{index:02d}.{suffix}",
                    "object_key": key,
                    "sha256": digest,
                    "size_bytes": len(payload),
                    "content_type": "application/octet-stream",
                }
            )
        await complete_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
            result_payload={"status": "succeeded", "outputs": outputs},
        )
        manifest = {
            "schema_version": "agent-staging-manifest.v1",
            "candidate_build_id": str(candidate.candidate_build_id),
            "workflow_run_id": str(workflow.workflow_id),
            "step_run_id": str(step.step_id),
            "execution_attempt_id": str(attempt.attempt_id),
            "source_id": str(source.source_id),
            "source_hash": source.source_hash,
            "plan_step_key": "model-main",
            "run_step_key": "model-main",
            "outputs": outputs,
            "runtime_provenance": {"image_digest": "sha256:" + "c" * 64},
        }
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
        await transition_step(
            connection,
            step.step_id,
            expected=StepStatus.RUNNING,
            target=StepStatus.SUCCEEDED,
        )
        evidence_ids = {}
        for index, (gate, mode, outcome) in enumerate(
            (
                ("geometry", "required", "passed"),
                ("visual", "advisory", "indeterminate"),
                ("dfm", "advisory", "failed"),
            )
        ):
            validation_step = await create_step(
                connection,
                tenant_id=owner.tenant_id,
                workflow_id=workflow.workflow_id,
                step_key=f"{gate}-model-main",
                step_index=20 + index,
                kind=f"agent_{gate}_validation",
            )
            recorded = await record_validation_evidence(
                connection,
                tenant_id=owner.tenant_id,
                candidate_build_id=candidate.candidate_build_id,
                workflow_id=workflow.workflow_id,
                step_id=validation_step.step_id,
                staging_manifest_id=accepted.staging_manifest_id,
                gate=gate,
                mode=mode,
                outcome=outcome,
                evidence={
                    "schema_version": f"controlled-{gate}.v1",
                    "issues": [f"{gate}_issue"] if outcome != "passed" else [],
                    "violations": [{"rule_id": "dfm-risk"}]
                    if gate == "dfm"
                    else [],
                },
            )
            evidence_ids[gate] = recorded.evidence_id
    selected = {
        "step_key": "model-main",
        "source_id": str(source.source_id),
        "source_hash": source.source_hash,
        "staging_manifest_id": str(accepted.staging_manifest_id),
        "manifest_hash": accepted.manifest_hash,
        "geometry_evidence_id": str(evidence_ids["geometry"]),
        "visual_evidence_id": str(evidence_ids["visual"]),
        "dfm_evidence_id": str(evidence_ids["dfm"]),
    }
    return {
        "owner": owner,
        "project_id": project_id,
        "initial": initial,
        "workflow_id": workflow.workflow_id,
        "candidate_id": candidate.candidate_build_id,
        "step_id": step.step_id,
        "source_id": source.source_id,
        "plan": plan.temporal_payload(),
        "selected": selected,
        "outputs": outputs,
    }


async def _seal(context, *, fault_hook=None):
    return await seal_agent_candidate(
        tenant_id=context["owner"].tenant_id,
        principal_id=context["owner"].principal_id,
        workflow_id=context["workflow_id"],
        candidate_build_id=context["candidate_id"],
        plan=context["plan"],
        selected_manifests=[context["selected"]],
        fault_hook=fault_hook,
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_candidate_seal_commits_once_and_preserves_advisory_risks():
    context = await _seed_selection()
    sealed = await _seal(context)
    replay = await _seal(context)
    assert sealed.replayed is False
    assert replay.replayed is True
    assert replay.seal_id == sealed.seal_id
    assert len(sealed.artifacts) == 2
    async with tenant_transaction(
        context["owner"].tenant_id,
        context["owner"].principal_id,
    ) as connection:
        state = (
            await connection.execute(
                text(
                    """
                    SELECT b.status, s.status AS seal_status,
                           c.validation_summary, c.risk_summary,
                           (SELECT count(*) FROM artifacts
                            WHERE revision_id=b.candidate_revision_id) artifacts,
                           (SELECT count(*) FROM agent_seal_manifests
                            WHERE seal_id=s.id) manifests,
                           (SELECT count(*) FROM agent_seal_evidence
                            WHERE seal_id=s.id) evidence
                    FROM agent_candidate_builds b
                    JOIN agent_candidate_seals s ON s.candidate_build_id=b.id
                    JOIN change_sets c ON c.id=b.change_set_id
                    WHERE b.id=:candidate_id
                    """
                ),
                {"candidate_id": context["candidate_id"]},
            )
        ).mappings().one()
    assert state["status"] == "reviewable"
    assert state["seal_status"] == "committed"
    assert state["artifacts"] == 2
    assert state["manifests"] == 1
    assert state["evidence"] == 3
    assert state["validation_summary"]["status"] == "passed"
    assert state["risk_summary"]["status"] == "attention_required"
    assert {item["gate"] for item in state["risk_summary"]["items"]} == {
        "visual",
        "dfm",
    }
    for artifact in sealed.artifacts:
        assert hashlib.sha256(await get_object(artifact.object_key)).hexdigest() == (
            artifact.sha256
        )


@pytest.mark.asyncio(loop_scope="module")
async def test_concurrent_seal_creates_one_revision_and_change_set():
    context = await _seed_selection()
    first, second = await asyncio.gather(_seal(context), _seal(context))
    assert first.seal_id == second.seal_id
    assert {first.replayed, second.replayed} == {False, True}
    async with tenant_transaction(
        context["owner"].tenant_id,
        context["owner"].principal_id,
    ) as connection:
        counts = (
            await connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM project_revisions "
                    "WHERE source_workflow_run_id=:id) revisions, "
                    "(SELECT count(*) FROM change_sets "
                    "WHERE source_workflow_run_id=:id) changes, "
                    "(SELECT count(*) FROM artifacts "
                    "WHERE workflow_run_id=:id) artifacts"
                ),
                {"id": context["workflow_id"]},
            )
        ).mappings().one()
    assert dict(counts) == {"revisions": 1, "changes": 1, "artifacts": 2}


@pytest.mark.asyncio(loop_scope="module")
async def test_tampered_staging_and_missing_required_evidence_fail_closed():
    tampered = await _seed_selection()
    await put_object(
        tampered["outputs"][0]["object_key"],
        b"tampered",
        content_type="application/octet-stream",
    )
    with pytest.raises(CandidateSealVerificationError, match="SHA-256"):
        await _seal(tampered)
    missing = await _seed_selection()
    missing["selected"]["geometry_evidence_id"] = None
    with pytest.raises(CandidateSealVerificationError, match="geometry evidence"):
        await _seal(missing)
    for context in (tampered, missing):
        async with tenant_transaction(
            context["owner"].tenant_id,
            context["owner"].principal_id,
        ) as connection:
            assert await connection.scalar(
                text("SELECT count(*) FROM artifacts WHERE workflow_run_id=:id"),
                {"id": context["workflow_id"]},
            ) == 0


@pytest.mark.asyncio(loop_scope="module")
async def test_cross_candidate_manifest_selection_is_rejected():
    target = await _seed_selection()
    foreign = await _seed_selection()
    target["selected"] = foreign["selected"]
    with pytest.raises(CandidateSealVerificationError, match="manifest"):
        await _seal(target)


@pytest.mark.asyncio(loop_scope="module")
async def test_superseded_manifest_selection_is_rejected():
    context = await _seed_selection()
    async with tenant_transaction(
        context["owner"].tenant_id,
        context["owner"].principal_id,
    ) as connection:
        repair_step = await create_step(
            connection,
            tenant_id=context["owner"].tenant_id,
            workflow_id=context["workflow_id"],
            step_key="repair-model-main-01",
            step_index=30,
            kind="agent_repair",
        )
        await transition_step(
            connection,
            repair_step.step_id,
            expected=StepStatus.PENDING,
            target=StepStatus.READY,
        )
        await transition_step(
            connection,
            repair_step.step_id,
            expected=StepStatus.READY,
            target=StepStatus.RUNNING,
        )
        attempt = await create_attempt(
            connection,
            tenant_id=context["owner"].tenant_id,
            workflow_id=context["workflow_id"],
            step_id=repair_step.step_id,
            idempotency_key=f"supersede-{context['candidate_id']}",
            execution_payload={"operation": "repair"},
        )
        lease = await lease_attempt(
            connection,
            attempt.attempt_id,
            worker_id="seal-test",
            lease_seconds=60,
        )
        await start_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
        )
        payload = b"superseding-step"
        digest = hashlib.sha256(payload).hexdigest()
        key = (
            f"staging/agent/tenants/{context['owner'].tenant_id}/candidates/"
            f"{context['candidate_id']}/attempts/{attempt.attempt_id}/"
            f"{digest}/geometry.step"
        )
        await put_object(key, payload, content_type="application/octet-stream")
        outputs = [
            {
                "format": "step",
                "filename": "geometry.step",
                "object_key": key,
                "sha256": digest,
                "size_bytes": len(payload),
                "content_type": "application/octet-stream",
            }
        ]
        await complete_attempt(
            connection,
            attempt.attempt_id,
            lease_token=lease.token,
            lease_generation=lease.generation,
            result_payload={"status": "succeeded", "outputs": outputs},
        )
        source = await record_generated_source(
            connection,
            tenant_id=context["owner"].tenant_id,
            candidate_build_id=context["candidate_id"],
            workflow_id=context["workflow_id"],
            step_id=repair_step.step_id,
            source_code="result = make_box(21, 10, 4)\n",
            generator_kind="controlled_repair",
            provider="controlled",
            model="controlled",
            request_hash="d" * 64,
            response_hash="e" * 64,
            predecessor_source_id=context["source_id"],
        )
        await accept_staging_manifest(
            connection,
            tenant_id=context["owner"].tenant_id,
            candidate_build_id=context["candidate_id"],
            workflow_id=context["workflow_id"],
            step_id=repair_step.step_id,
            attempt_id=attempt.attempt_id,
            lease_generation=lease.generation,
            lease_token=lease.token,
            supersedes_id=context["selected"]["staging_manifest_id"],
            manifest={
                "schema_version": "agent-staging-manifest.v1",
                "candidate_build_id": str(context["candidate_id"]),
                "workflow_run_id": str(context["workflow_id"]),
                "step_run_id": str(repair_step.step_id),
                "execution_attempt_id": str(attempt.attempt_id),
                "source_id": str(source.source_id),
                "source_hash": source.source_hash,
                "plan_step_key": "model-main",
                "run_step_key": "repair-model-main-01",
                "outputs": outputs,
                "runtime_provenance": {"image_digest": "sha256:" + "c" * 64},
            },
        )
    with pytest.raises(CandidateSealVerificationError, match="stale, consumed"):
        await _seal(context)


@pytest.mark.asyncio(loop_scope="module")
async def test_stale_base_copy_is_not_committed_and_orphan_is_reconciled():
    context = await _seed_selection()
    initial = context["initial"]
    async with tenant_transaction(
        context["owner"].tenant_id,
        context["owner"].principal_id,
    ) as connection:
        sibling = await create_candidate_change_set(
            connection,
            tenant_id=context["owner"].tenant_id,
            project_id=context["project_id"],
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            created_by_principal_id=context["owner"].principal_id,
            idempotency_key=f"seal-sibling-{context['project_id']}",
            objective="并发推进分支",
            candidate_manifest={"state": "advanced"},
        )
        assert await compare_and_swap_branch_head(
            connection,
            tenant_id=context["owner"].tenant_id,
            project_id=context["project_id"],
            branch_id=initial.branch_id,
            expected_head_revision_id=initial.revision_id,
            candidate_revision_id=sibling.candidate_revision_id,
        )
    with pytest.raises(StaleBaseRevision):
        await _seal(context)
    async with tenant_transaction(
        context["owner"].tenant_id,
        context["owner"].principal_id,
    ) as connection:
        assert await connection.scalar(
            text("SELECT count(*) FROM artifacts WHERE workflow_run_id=:id"),
            {"id": context["workflow_id"]},
        ) == 0
    cleanup = await cleanup_artifact_orphans(
        tenant_id=context["owner"].tenant_id,
        principal_id=context["owner"].principal_id,
        grace_seconds=0,
    )
    assert cleanup["deleted_final_orphans"] == len(context["outputs"])


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("failure_point", ["after_partial_copy", "after_all_copies"])
async def test_copy_crash_reconciles_without_duplicate_products(failure_point):
    context = await _seed_selection()

    def crash(point):
        if point == failure_point:
            raise RuntimeError(f"controlled:{point}")

    with pytest.raises(RuntimeError, match="controlled"):
        await _seal(context, fault_hook=crash)
    sealed = await _seal(context)
    assert len(sealed.artifacts) == 2
    async with tenant_transaction(
        context["owner"].tenant_id,
        context["owner"].principal_id,
    ) as connection:
        counts = (
            await connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM artifacts WHERE workflow_run_id=:id) "
                    "AS artifacts, (SELECT count(*) FROM change_sets "
                    "WHERE source_workflow_run_id=:id) AS changes"
                ),
                {"id": context["workflow_id"]},
            )
        ).mappings().one()
    assert dict(counts) == {"artifacts": 2, "changes": 1}


@pytest.mark.asyncio(loop_scope="module")
async def test_cleanup_crash_reconciles_committed_seal():
    context = await _seed_selection()

    def crash(point):
        if point == "after_db_commit":
            raise RuntimeError("controlled:after_db_commit")

    with pytest.raises(RuntimeError, match="after_db_commit"):
        await _seal(context, fault_hook=crash)
    assert all(
        [await get_object(item["object_key"]) for item in context["outputs"]]
    )
    replay = await _seal(context)
    assert replay.replayed is True
    for output in context["outputs"]:
        with pytest.raises(Exception):
            await get_object(output["object_key"])
