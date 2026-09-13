"""Real PostgreSQL acceptance: no mocked database, queue, permissions or CAS."""
import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import settings
from app.db import close_database, tenant_transaction
from app.domain.identity import user_principal
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.repositories.revisions import create_initial_branch, create_candidate_change_set, compare_and_swap_branch_head
from app.services.run_state import create_workflow
from app.services.cloud_documents import (DocumentConflict, acquire_operation, add_comment,
    document_snapshot, document_events, touch_presence, collaboration_snapshot)
from app.services.document_sharing import create_review_invite, accept_review_invite, workspace_principal, change_project_member, project_members

URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
pytestmark = [pytest.mark.skipif(not URL, reason="requires isolated CAD_AGENT_TEST_DATABASE_URL"),
              pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(scope="module", autouse=True)
def migrate():
    if URL:
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", URL.replace("%", "%%"))
        command.upgrade(config, "head")


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def engine(migrate):
    old = settings.database_url
    settings.database_url = URL
    yield
    await close_database()
    settings.database_url = old


async def seed():
    owner = user_principal(f"cloud-{uuid4()}")
    project = uuid4()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        await ensure_principal(conn, owner)
        await create_project(conn, project_id=project, tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id, name="Cloud test", slug=f"cloud-{project.hex}")
        branch = await create_initial_branch(conn, tenant_id=owner.tenant_id, project_id=project,
            created_by_principal_id=owner.principal_id, branch_name="main", initial_manifest={})
    return owner, project, branch


async def enqueue(owner, project, branch, *, key=None, version=0):
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        return await create_workflow(conn, tenant_id=owner.tenant_id, project_id=project,
            requested_by_principal_id=owner.principal_id, kind="mcad.generate",
            idempotency_key=key or str(uuid4()), request_payload={"branch_id": str(branch.branch_id),
                "expected_base_revision_id": str(branch.revision_id), "expected_state_version": version,
                "operation": "generate", "objective": "test transaction"})


async def test_real_queue_serializes_and_replays_without_duplicate_mutations():
    owner, project, branch = await seed()
    first = await enqueue(owner, project, branch, key=f"replay-{branch.branch_id}")
    replay = await enqueue(owner, project, branch, key=f"replay-{branch.branch_id}")
    second = await enqueue(owner, project, branch)
    assert first.workflow_id == replay.workflow_id and replay.replayed
    results = await asyncio.gather(*(acquire_operation(owner.tenant_id, owner.principal_id, w.workflow_id) for w in (second, first)))
    assert [r["status"] for r in results] == ["waiting", "acquired"]
    assert (await acquire_operation(owner.tenant_id, owner.principal_id, first.workflow_id))["status"] == "acquired"
    # Simulate the durable terminal boundary before the worker's release call.
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        await conn.execute(text("UPDATE workflow_runs SET status='failed', error_code='kernel_crash' WHERE id=:id"), {"id": first.workflow_id})
    assert (await acquire_operation(owner.tenant_id, owner.principal_id, second.workflow_id))["status"] == "acquired"
    assert (await document_snapshot(owner, branch.branch_id))["state_version"] == 0


async def test_head_commit_rollback_delta_and_aba_stale_state():
    owner, project, branch = await seed()
    queued = await enqueue(owner, project, branch)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        candidate = await create_candidate_change_set(conn, tenant_id=owner.tenant_id, project_id=project,
            branch_id=branch.branch_id, expected_base_revision_id=branch.revision_id,
            created_by_principal_id=owner.principal_id, objective="revision", idempotency_key=str(uuid4()), candidate_manifest={"version": 2})
        assert await compare_and_swap_branch_head(conn, tenant_id=owner.tenant_id, project_id=project,
            branch_id=branch.branch_id, expected_head_revision_id=branch.revision_id, candidate_revision_id=candidate.candidate_revision_id)
    assert (await document_snapshot(owner, branch.branch_id))["state_version"] == 1
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        await conn.execute(text("UPDATE project_branches SET head_revision_id=:head WHERE id=:id"), {"head": branch.revision_id, "id": branch.branch_id})
    snapshot = await document_snapshot(owner, branch.branch_id)
    assert snapshot["head_revision_id"] == str(branch.revision_id) and snapshot["state_version"] == 2
    with pytest.raises(DocumentConflict):
        await enqueue(owner, project, branch, version=0)
    assert (await acquire_operation(owner.tenant_id, owner.principal_id, queued.workflow_id))["error_code"] == "document_state_stale"
    events = await document_events(owner, branch.branch_id, 0)
    assert [e["payload"]["state_version"] for e in events] == [1, 2]
    assert events[-1]["payload"]["upserted"] == []


async def test_candidate_cas_rejects_old_generation_after_head_returns_to_same_revision():
    """Real database ABA regression at the final shared commit boundary."""
    owner, project, branch = await seed()

    async def candidate(label):
        async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
            return await create_candidate_change_set(
                conn, tenant_id=owner.tenant_id, project_id=project,
                branch_id=branch.branch_id, expected_base_revision_id=branch.revision_id,
                created_by_principal_id=owner.principal_id, objective=label,
                idempotency_key=str(uuid4()), candidate_manifest={"change": label})

    old = await candidate("waiting for review at generation zero")
    other = await candidate("concurrent candidate")
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        assert await compare_and_swap_branch_head(
            conn, tenant_id=owner.tenant_id, project_id=project, branch_id=branch.branch_id,
            expected_head_revision_id=branch.revision_id, candidate_revision_id=other.candidate_revision_id)
        await conn.execute(text("UPDATE project_branches SET head_revision_id=:head WHERE id=:id"),
                           {"head": branch.revision_id, "id": branch.branch_id})
    snapshot = await document_snapshot(owner, branch.branch_id)
    assert snapshot["head_revision_id"] == str(branch.revision_id) and snapshot["state_version"] == 2
    from app.services.change_sets import accept_change_set, commit_change_set, ChangeSetStateConflict
    # Staleness must stop review before missing artifact evidence could mask it.
    with pytest.raises(ChangeSetStateConflict, match="stale"):
        await accept_change_set(tenant_id=owner.tenant_id, reviewer_principal_id=owner.principal_id,
                                change_set_id=old.change_set_id)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        # Persist an already-accepted review to test the commit boundary alone.
        await conn.execute(text("UPDATE change_sets SET status='accepted' WHERE id=:id"), {"id": old.change_set_id})
    with pytest.raises(ChangeSetStateConflict, match="stale"):
        await commit_change_set(tenant_id=owner.tenant_id, reviewer_principal_id=owner.principal_id,
                                change_set_id=old.change_set_id)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        assert not await compare_and_swap_branch_head(
            conn, tenant_id=owner.tenant_id, project_id=project, branch_id=branch.branch_id,
            expected_head_revision_id=branch.revision_id, candidate_revision_id=old.candidate_revision_id)
    assert (await document_snapshot(owner, branch.branch_id))["state_version"] == 2

    fresh = await candidate("explicit new candidate at generation two")
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        assert await compare_and_swap_branch_head(
            conn, tenant_id=owner.tenant_id, project_id=project, branch_id=branch.branch_id,
            expected_head_revision_id=branch.revision_id, candidate_revision_id=fresh.candidate_revision_id)
    assert (await document_snapshot(owner, branch.branch_id))["state_version"] == 3


async def test_merge_candidate_rejects_source_aba_at_review_and_commit():
    from app.services.change_sets import accept_change_set, ChangeSetStateConflict
    owner, project, target = await seed()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        source = await create_initial_branch(conn, tenant_id=owner.tenant_id, project_id=project,
            created_by_principal_id=owner.principal_id, branch_name="source", initial_manifest={})
        workflow = await create_workflow(conn, tenant_id=owner.tenant_id, project_id=project,
            requested_by_principal_id=owner.principal_id, kind="merge",
            idempotency_key=str(uuid4()), request_payload={"objective": "database merge boundary"})
        await conn.execute(text("""INSERT INTO document_merges(id,tenant_id,project_id,document_id,
            source_document_id,source_revision_id,source_state_version,target_revision_id,target_state_version,
            common_revision_id,mode,principal_id,idempotency_key,request_hash,proposal,workflow_run_id)
            VALUES(:id,:tenant,:project,:target,:source,:source_rev,0,:target_rev,0,:target_rev,
            'source_geometry',:principal,:key,:hash,'{}',:workflow)"""), {
                "id": uuid4(), "tenant": owner.tenant_id, "project": project,
                "target": target.branch_id, "source": source.branch_id, "source_rev": source.revision_id,
                "target_rev": target.revision_id, "principal": owner.principal_id,
                "key": str(uuid4()), "hash": "a" * 64, "workflow": workflow.workflow_id})
        merged = await create_candidate_change_set(conn, tenant_id=owner.tenant_id, project_id=project,
            branch_id=target.branch_id, expected_base_revision_id=target.revision_id,
            created_by_principal_id=owner.principal_id, objective="merge", idempotency_key=str(uuid4()),
            source_workflow_run_id=workflow.workflow_id, candidate_manifest={"merged": True})
        changed = await create_candidate_change_set(conn, tenant_id=owner.tenant_id, project_id=project,
            branch_id=source.branch_id, expected_base_revision_id=source.revision_id,
            created_by_principal_id=owner.principal_id, objective="source change", idempotency_key=str(uuid4()),
            candidate_manifest={"source": "changed"})
        assert await compare_and_swap_branch_head(conn, tenant_id=owner.tenant_id, project_id=project,
            branch_id=source.branch_id, expected_head_revision_id=source.revision_id,
            candidate_revision_id=changed.candidate_revision_id)
        await conn.execute(text("UPDATE project_branches SET head_revision_id=:revision WHERE id=:id"),
                           {"revision": source.revision_id, "id": source.branch_id})
    with pytest.raises(ChangeSetStateConflict, match="stale merge source"):
        await accept_change_set(tenant_id=owner.tenant_id, reviewer_principal_id=owner.principal_id,
                                change_set_id=merged.change_set_id)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        assert not await compare_and_swap_branch_head(conn, tenant_id=owner.tenant_id, project_id=project,
            branch_id=target.branch_id, expected_head_revision_id=target.revision_id,
            candidate_revision_id=merged.candidate_revision_id)
    assert (await document_snapshot(owner, target.branch_id))["state_version"] == 0


async def test_candidate_generation_cannot_be_rewritten():
    owner, project, branch = await seed()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        candidate = await create_candidate_change_set(conn, tenant_id=owner.tenant_id, project_id=project,
            branch_id=branch.branch_id, expected_base_revision_id=branch.revision_id,
            created_by_principal_id=owner.principal_id, objective="immutable generation",
            idempotency_key=str(uuid4()), candidate_manifest={})
    with pytest.raises(Exception, match="generation is immutable"):
        async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
            await conn.execute(text("UPDATE change_sets SET base_state_version=99 WHERE id=:id"),
                               {"id": candidate.change_set_id})


async def test_projection_upgrade_preserves_immutable_v1_v2_and_v3_caches():
    owner, project, branch = await seed()
    import json
    legacy = {"schema_version": "cad-semantic-state.v1", "features": [], "legacy_evidence": "retained"}
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        await conn.execute(text("""INSERT INTO document_checkpoints(tenant_id,document_id,revision_id,projection)
            VALUES(:tenant,:doc,:rev,CAST(:projection AS jsonb))"""),
            {"tenant": owner.tenant_id, "doc": branch.branch_id, "rev": branch.revision_id, "projection": json.dumps(legacy)})
        await conn.execute(text("""INSERT INTO document_checkpoints(tenant_id,document_id,revision_id,projector_version,projection)
            VALUES(:tenant,:doc,:rev,2,CAST(:projection AS jsonb))"""),
            {"tenant": owner.tenant_id, "doc": branch.branch_id, "rev": branch.revision_id, "projection": json.dumps(legacy)})
        await conn.execute(text("""INSERT INTO document_checkpoints(tenant_id,document_id,revision_id,projector_version,projection)
            VALUES(:tenant,:doc,:rev,3,CAST(:projection AS jsonb))"""),
            {"tenant": owner.tenant_id, "doc": branch.branch_id, "rev": branch.revision_id, "projection": json.dumps(legacy)})
    snapshot = await document_snapshot(owner, branch.branch_id)
    assert snapshot["projector_version"] == 4 and snapshot["features"] == []
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        rows = (await conn.execute(text("SELECT projector_version,projection FROM document_checkpoints WHERE document_id=:doc ORDER BY projector_version"),
                                  {"doc": branch.branch_id})).mappings().all()
        assert [r["projector_version"] for r in rows] == [1, 2, 3, 4]
        assert rows[0]["projection"] == rows[1]["projection"] == rows[2]["projection"] == legacy
    with pytest.raises(Exception, match="document evidence is immutable"):
        async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
            await conn.execute(text("UPDATE document_checkpoints SET projection='{}'::jsonb WHERE document_id=:doc"), {"doc": branch.branch_id})


async def test_viewer_presence_comments_and_tenant_isolation():
    owner, project, branch = await seed()
    viewer = user_principal(f"viewer-{uuid4()}", tenant_id=owner.tenant_id)
    outsider = user_principal(f"outsider-{uuid4()}")
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        await ensure_principal(conn, viewer)
        await conn.execute(text("INSERT INTO project_memberships(tenant_id, project_id, principal_id, role) VALUES(:t,:p,:u,'viewer')"),
            {"t": owner.tenant_id, "p": project, "u": viewer.principal_id})
    assert not (await document_snapshot(viewer, branch.branch_id))["can_edit"]
    with pytest.raises(KeyError):
        await document_snapshot(outsider, branch.branch_id)
    comment = dict(revision_id=branch.revision_id, body="Review note", feature_id=None, comment_id=uuid4())
    assert not (await add_comment(viewer, branch.branch_id, **comment))["replayed"]
    assert (await add_comment(viewer, branch.branch_id, **comment))["replayed"]
    client = uuid4()
    await touch_presence(viewer, branch.branch_id, client, None)
    with pytest.raises(PermissionError):
        await touch_presence(owner, branch.branch_id, client, None)
    collaboration = await collaboration_snapshot(owner, branch.branch_id)
    assert len(collaboration["comments"]) == 1 and len(collaboration["presence"]) == 1
    job = await enqueue(viewer, project, branch)
    assert (await acquire_operation(viewer.tenant_id, viewer.principal_id, job.workflow_id))["error_code"] == "permission_denied"


async def test_real_cross_account_invitation_does_not_grant_tenant_ownership():
    owner, project, branch = await seed()
    guest = user_principal(f"guest-{uuid4()}")
    other = user_principal(f"other-{uuid4()}")
    with pytest.raises(PermissionError):
        await workspace_principal(guest, owner.tenant_id)
    invitation = await create_review_invite(owner, branch.branch_id)
    await accept_review_invite(guest, branch.branch_id, owner.tenant_id, invitation["token"])
    await accept_review_invite(guest, branch.branch_id, owner.tenant_id, invitation["token"])
    with pytest.raises(PermissionError):
        await accept_review_invite(other, branch.branch_id, owner.tenant_id, invitation["token"])
    invited = await workspace_principal(guest, owner.tenant_id)
    snapshot = await document_snapshot(invited, branch.branch_id)
    assert not snapshot["can_edit"] and not snapshot["can_share"]
    assert (await document_snapshot(owner, branch.branch_id))["can_share"]
    with pytest.raises(PermissionError):
        await create_review_invite(invited, branch.branch_id)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        assert await conn.scalar(text("SELECT role FROM tenant_memberships WHERE principal_id=:id"), {"id": invited.principal_id}) == "member"
        stored = await conn.scalar(text("SELECT token_hash FROM document_review_invites WHERE document_id=:doc"), {"doc": branch.branch_id})
        assert stored != invitation["token"]
        await conn.execute(text("DELETE FROM tenant_memberships WHERE tenant_id=:t AND principal_id=:p"),
            {"t": owner.tenant_id, "p": invited.principal_id})
    # Includes callers holding the previously authorized context, such as an
    # already-open stream. Project membership alone cannot retain access.
    with pytest.raises(PermissionError):
        await document_events(invited, branch.branch_id, 0)
    with pytest.raises(PermissionError):
        await accept_review_invite(guest, branch.branch_id, owner.tenant_id, invitation["token"])


async def test_queued_operation_cannot_run_after_workspace_membership_is_revoked():
    owner, project, branch = await seed()
    queued = await enqueue(owner, project, branch)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        await conn.execute(text("DELETE FROM tenant_memberships WHERE tenant_id=:t AND principal_id=:p"),
            {"t": owner.tenant_id, "p": owner.principal_id})
    result = await acquire_operation(owner.tenant_id, owner.principal_id, queued.workflow_id)
    assert result == {"status": "rejected", "error_code": "permission_denied"}


async def test_late_cancelled_attempt_cannot_cancel_its_replacement_step():
    from app.services.run_state import create_step, create_attempt
    from app.execution.contracts import ExecutionStatus
    from app.workflows.activities import _mark_execution_failure

    owner, project, branch = await seed()
    run = await enqueue(owner, project, branch)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        step = await create_step(conn, tenant_id=owner.tenant_id, workflow_id=run.workflow_id,
            step_key="model", step_index=0, kind="agent_model")
        first = await create_attempt(conn, tenant_id=owner.tenant_id, workflow_id=run.workflow_id,
            step_id=step.step_id, idempotency_key=f"old-{run.workflow_id}", execution_payload={"attempt": 1})
        # Real persisted boundary after the retry fenced the older worker.
        await conn.execute(text("UPDATE execution_attempts SET status='failed', error_code='superseded_by_temporal_retry' WHERE id=:id"), {"id": first.attempt_id})
        second = await create_attempt(conn, tenant_id=owner.tenant_id, workflow_id=run.workflow_id,
            step_id=step.step_id, idempotency_key=f"new-{run.workflow_id}", execution_payload={"attempt": 2})
        await conn.execute(text("UPDATE step_runs SET status='running' WHERE id=:id"), {"id": step.step_id})
        await conn.execute(text("UPDATE execution_attempts SET status='running' WHERE id=:id"), {"id": second.attempt_id})
    await _mark_execution_failure({"tenant_id": str(owner.tenant_id), "principal_id": str(owner.principal_id)},
        attempt_id=first.attempt_id, step_id=step.step_id, status=ExecutionStatus.CANCELLED,
        error_code="late_cancellation", error_message="Expired worker noticed cancellation")
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        assert await conn.scalar(text("SELECT status FROM step_runs WHERE id=:id"), {"id": step.step_id}) == "running"
        assert await conn.scalar(text("SELECT status FROM execution_attempts WHERE id=:id"), {"id": second.attempt_id}) == "running"


async def test_editor_invite_grants_project_edit_only_and_replay_cannot_restore_downgraded_role():
    from app.domain.projects import Permission
    from app.services.cloud_documents import authorized_document
    owner, project, branch = await seed()
    guest = user_principal(f'editor-{uuid4()}')
    invitation = await create_review_invite(owner, branch.branch_id, role='editor')
    assert (await accept_review_invite(guest, branch.branch_id, owner.tenant_id, invitation['token']))['role']=='editor'
    member = await workspace_principal(guest, owner.tenant_id)
    snapshot = await document_snapshot(member, branch.branch_id)
    assert snapshot['can_edit'] and not snapshot['can_share'] and not snapshot['can_commit']
    with pytest.raises(PermissionError):
        await create_review_invite(member, branch.branch_id, role='editor')
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        assert await conn.scalar(text('SELECT role FROM tenant_memberships WHERE principal_id=:id'), {'id':member.principal_id})=='member'
    await change_project_member(owner,branch.branch_id,member.principal_id,expected_role='editor',role='viewer')
    assert next(m for m in await project_members(owner,branch.branch_id) if m['principal_id']==member.principal_id)['role']=='viewer'
    with pytest.raises(DocumentConflict):
        await change_project_member(owner,branch.branch_id,member.principal_id,expected_role='editor',role='viewer')
    replay = await accept_review_invite(guest, branch.branch_id, owner.tenant_id, invitation['token'])
    assert replay['role']=='viewer'
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        with pytest.raises(PermissionError):
            await authorized_document(conn, member, branch.branch_id, Permission.MODIFY_DESIGN)
    await change_project_member(owner,branch.branch_id,member.principal_id,expected_role='viewer')
    with pytest.raises(PermissionError):
        await document_snapshot(member,branch.branch_id)
    with pytest.raises(PermissionError):
        await accept_review_invite(guest,branch.branch_id,owner.tenant_id,invitation['token'])
