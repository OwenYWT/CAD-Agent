"""Real PostgreSQL/Temporal dispatch recovery, without service replacements."""
import asyncio
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db import close_database, tenant_transaction
from app.domain.identity import user_principal
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.repositories.revisions import create_initial_branch
from app.services.run_state import create_workflow
from app.services.workflow_dispatch import (persist_dispatch,claim_dispatch,finish_claim,
    dispatch_once,start_dispatch,_dispatcher_transaction)
from app.workflows.temporal import McadWorkflowRequest,McadExecutionRequest,McadOutputRequest,temporal_workflow_id

URL=os.getenv("CAD_AGENT_TEST_DATABASE_URL","")
pytestmark=[pytest.mark.skipif(not URL,reason="requires isolated PostgreSQL"),pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(scope="module",autouse=True)
def migrate():
    if URL:
        config=Config(str(Path(__file__).resolve().parents[2]/"alembic.ini"))
        config.set_main_option("sqlalchemy.url",URL.replace("%","%%"))
        command.upgrade(config,"head")


@pytest_asyncio.fixture(scope="module",loop_scope="module",autouse=True)
async def engine(migrate):
    before=settings.database_url;settings.database_url=URL
    yield
    await close_database();settings.database_url=before


async def prepare(*,rollback=False):
    owner=user_principal(f"dispatch-{uuid4()}"); project=uuid4(); queue=f"dispatch-{uuid4()}"
    primary=McadExecutionRequest(step_key="model",kind="mcad_model",operation="execute",
        source_code="import cadquery as cq\nresult = cq.Workplane('XY').box(20, 10, 4)\n",
        outputs=(McadOutputRequest(name="step",media_type="model/step"),McadOutputRequest(name="stl",media_type="model/stl")))
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        await ensure_principal(conn,owner)
        await create_project(conn,project_id=project,tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id,name="Dispatch",slug=project.hex)
        branch=await create_initial_branch(conn,tenant_id=owner.tenant_id,project_id=project,
            created_by_principal_id=owner.principal_id,branch_name="main",initial_manifest={})
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        created=await create_workflow(conn,tenant_id=owner.tenant_id,project_id=project,
            requested_by_principal_id=owner.principal_id,kind="mcad.execute",idempotency_key=str(uuid4()),
            request_payload={"branch_id":str(branch.branch_id),"expected_base_revision_id":str(branch.revision_id),
                "objective":"actual box", "primary":primary.model_dump(mode="json")})
        request=McadWorkflowRequest(workflow_run_id=created.workflow_id,tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,project_id=project,branch_id=branch.branch_id,
            expected_base_revision_id=branch.revision_id,document_queue=True,primary=primary,
            objective="actual box",require_confirmation=False,commit_after_confirmation=False)
        row=await persist_dispatch(conn,tenant_id=owner.tenant_id,principal_id=owner.principal_id,
            workflow_id=created.workflow_id,workflow_type="McadDurableWorkflow",
            temporal_id=temporal_workflow_id(created.workflow_id),task_queue=queue,payload=request.temporal_payload())
        if rollback:
            await conn.rollback()
    return owner,row


async def test_dispatch_is_atomic_with_workflow_and_queue():
    owner,row=await prepare(rollback=True)
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        for table,column in (("workflow_runs","id"),("cad_operations","id"),("workflow_dispatches","workflow_id")):
            assert await conn.scalar(text(f"SELECT count(*) FROM {table} WHERE {column}=:id"),{"id":row['workflow_id']})==0


async def test_dispatch_leases_fence_expired_dispatchers():
    owner,row=await prepare()
    results=await asyncio.gather(*(claim_dispatch(task_queues=(row['task_queue'],)) for _ in range(2)))
    winners=[r for r in results if r];assert len(winners)==1
    first=winners[0]
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        await conn.execute(text("UPDATE workflow_dispatches SET lease_until=CURRENT_TIMESTAMP-interval '1 second' WHERE workflow_id=:id"),{"id":row['workflow_id']})
    second=await claim_dispatch(task_queues=(row['task_queue'],));assert second['lease_token']!=first['lease_token']
    await finish_claim(first)
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        assert await conn.scalar(text("SELECT status FROM workflow_dispatches WHERE workflow_id=:id"),{"id":row['workflow_id']})=='dispatching'
    await finish_claim(second)
    assert await claim_dispatch(task_queues=(row['task_queue'],)) is None


async def test_dispatch_payload_is_immutable_and_runtime_is_tenant_isolated():
    owner,row=await prepare()
    other=user_principal(f"outside-{uuid4()}")
    async with tenant_transaction(other.tenant_id,other.principal_id) as conn:
        assert await conn.scalar(text("SELECT count(*) FROM workflow_dispatches WHERE workflow_id=:id"),{"id":row['workflow_id']})==0
    with pytest.raises(DBAPIError):
        async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
            await conn.execute(text("UPDATE workflow_dispatches SET payload='{}' WHERE workflow_id=:id"),{"id":row['workflow_id']})
    with pytest.raises(DBAPIError):
        async with _dispatcher_transaction() as conn:
            await conn.execute(text("SELECT id FROM workflow_runs"))


@pytest.mark.skipif(os.getenv("CAD_AGENT_TEST_TEMPORAL")!="1",reason="requires real Temporal and sandbox")
async def test_committed_intent_starts_real_kernel_without_client_retry(monkeypatch):
    from app.temporal_client import get_temporal_client,reset_temporal_client
    from app.workers.workflow_worker import build_workflow_worker
    from app.execution.composition import get_execution_backend
    owner,row=await prepare()
    monkeypatch.setattr(settings,"temporal_task_queue",row['task_queue'])
    from app.workflows.temporal import _dispatch_after_commit
    from app.services.workflow_dispatch import dispatcher_readiness
    await dispatcher_readiness()
    real_target=settings.temporal_target
    reset_temporal_client()
    monkeypatch.setattr(settings,"temporal_target","127.0.0.1:1")
    assert await _dispatch_after_commit(row) is None
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        assert await conn.scalar(text("SELECT status FROM workflow_runs WHERE id=:id"),{"id":row['workflow_id']})=='pending'
    monkeypatch.setattr(settings,"temporal_target",real_target)
    reset_temporal_client()
    client=await get_temporal_client()
    try:
        async with build_workflow_worker(client,backend=get_execution_backend()):
            assert await dispatch_once(client,task_queues=(row['task_queue'],))
            # Starting again after delivery but before a hypothetical crash ack
            # reaches the same real Temporal history, including completed runs.
            handle=await start_dispatch(client,row)
            result=await asyncio.wait_for(handle.result(),timeout=180)
            assert result['status']=='succeeded'
            assert not await dispatch_once(client,task_queues=(row['task_queue'],))
        async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
            assert await conn.scalar(text("SELECT status FROM workflow_dispatches WHERE workflow_id=:id"),{"id":row['workflow_id']})=='dispatched'
            assert await conn.scalar(text("SELECT count(*) FROM execution_attempts WHERE workflow_run_id=:id"),{"id":row['workflow_id']})==1
            assert await conn.scalar(text("SELECT count(*) FROM artifacts WHERE workflow_run_id=:id"),{"id":row['workflow_id']})==2
        from app.services.durable_submission import DurableSubmission,wait_for_compatibility_response
        from uuid import UUID
        submission=DurableSubmission(row['workflow_id'],None,UUID(row['payload']['project_id']),
            UUID(row['payload']['branch_id']),UUID(row['payload']['expected_base_revision_id']),'execute')
        response=await wait_for_compatibility_response(owner,submission,timeout_seconds=1)
        assert response.success and response.task_status=='succeeded'
    finally:
        reset_temporal_client()
