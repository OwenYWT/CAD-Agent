"""Real DB/Temporal orchestration tests. Controlled operations are test fixtures."""

from app.services import llm_usage
import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from temporalio.exceptions import ApplicationError

from app.config import settings
from app.db import close_database, tenant_transaction
from app.domain.identity import user_principal
from app.domain.runs import WorkflowStatus, StepStatus
from app.repositories.identity import reconcile_principal
from app.repositories.projects import create_project
from app.services import model_jobs as jobs
from app.services.run_state import create_workflow, create_step, transition_step, transition_workflow
from app.model_job_context import ModelJobContext, model_job_context
from app.services.llm_usage import usage_context

pytestmark = pytest.mark.skipif(not os.getenv('CAD_MODEL_JOB_TEST_DATABASE_URL'), reason='isolated migrated PostgreSQL required')


@pytest_asyncio.fixture
async def request_job(monkeypatch):
    await close_database()
    monkeypatch.setattr(settings,'database_url',os.environ['CAD_MODEL_JOB_TEST_DATABASE_URL'])
    monkeypatch.setattr(settings,'durable_control_plane_enabled',True)
    owner = user_principal(str(uuid4()))
    await reconcile_principal(owner)
    project = uuid4()
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        await create_project(conn,project_id=project,tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id,name='model job test',slug=str(project))
        run = await create_workflow(conn,tenant_id=owner.tenant_id,project_id=project,
            requested_by_principal_id=owner.principal_id,kind='mcad.agent.v2.generate',
            idempotency_key=str(uuid4()),request_payload={})
    request = {'job_id':str(uuid4()),'operation':'agent_v2.requirements','payload':{
        'tenant_id':str(owner.tenant_id),'principal_id':str(owner.principal_id),
        'workflow_run_id':str(run.workflow_id)}}
    yield request
    p = request['payload']
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        await conn.execute(text("UPDATE model_jobs SET cancel_requested=true,status='cancelled' WHERE workflow_run_id=:id AND status IN ('queued','running')"),{'id':run.workflow_id})
    await close_database()


@pytest.mark.asyncio
async def test_idempotent_submission_and_conflicting_input(request_job):
    assert (await jobs.submit(request_job))['status']=='queued'
    assert (await jobs.submit(request_job))['status']=='queued'
    changed={**request_job,'payload':{**request_job['payload'],'different':'input'}}
    with pytest.raises(ApplicationError,match='input changed'):
        await jobs.submit(changed)


@pytest.mark.asyncio
async def test_numeric_payload_and_result_survive_real_database_roundtrip(request_job):
    from app.execution.canonical import canonical_sha256
    numbers = {'large': 4e100, 'large_finite_dimension': 1.2345678901234567e20,
               'precise': 1.2345678901234567, 'tiny': 5e-324,
               'integer': 2**53-1, 'text': '123456789012345678901234567890'}
    request_job['payload']['measurements'] = numbers
    await jobs.submit(request_job)
    job = await jobs.claim()
    assert job['payload']['measurements'] == numbers
    assert type(job['payload']['measurements']['large']) is float
    assert canonical_sha256({'operation': job['operation'], 'payload': job['payload']}) == job['payload_hash']
    async def operation(payload):
        return {'measurements': payload['measurements']}
    await jobs.execute(job, {request_job['operation']: operation})
    result = await jobs.read(request_job)
    assert result['status'] == 'succeeded'
    assert result['result']['measurements'] == numbers
    assert type(result['result']['measurements']['large']) is float
    assert type(result['result']['measurements']['integer']) is int
    assert (await jobs.submit(request_job))['status'] == 'succeeded'


@pytest.mark.asyncio
async def test_logical_step_replay_preserves_numeric_result(request_job):
    from uuid import UUID
    from app.workflows.activities import _start_agent_logical_step, _complete_agent_logical_step, _stored_agent_step_result
    p = request_job['payload']
    async with tenant_transaction(UUID(p['tenant_id']), UUID(p['principal_id'])) as conn:
        ids = dict(tenant_id=UUID(p['tenant_id']), workflow_id=UUID(p['workflow_run_id']))
        step = await _start_agent_logical_step(conn, **ids, step_key='numeric-replay',
                                             step_index=0, kind='agent_requirements')
        result = {'step_key': 'numeric-replay', 'large': 4e100, 'precise': 1.2345678901234567}
        await _complete_agent_logical_step(conn, **ids, step_id=step,
            event_type='agent.requirements.completed', result=result)
        replay = await _stored_agent_step_result(conn, workflow_id=ids['workflow_id'],
            event_type='agent.requirements.completed', step_key='numeric-replay')
    assert replay == {**result, 'replayed': True}
    assert type(replay['large']) is float


@pytest.mark.asyncio
async def test_cancellation_closes_operation_and_preserves_account(request_job,monkeypatch):
    monkeypatch.setattr(jobs,'HEARTBEAT_SECONDS',0.03)
    await jobs.submit(request_job)
    job=await jobs.claim()
    assert str(job['id'])==request_job['job_id']
    started=asyncio.Event();closed=asyncio.Event()
    async def operation(payload):
        assert str(usage_context.get().principal_id)==payload['principal_id']
        started.set()
        try: await asyncio.Event().wait()
        finally: closed.set()
    task=asyncio.create_task(jobs.execute(job,{request_job['operation']:operation}))
    await asyncio.wait_for(started.wait(),5)
    await jobs.cancel(request_job)
    await asyncio.wait_for(task,5)
    assert closed.is_set()
    assert (await jobs.read(request_job))['status']=='cancelled'


@pytest.mark.asyncio
async def test_expired_worker_lease_fences_old_writes(request_job):
    await jobs.submit(request_job);old=await jobs.claim()
    async with tenant_transaction(old['tenant_id'],old['principal_id']) as conn:
        await conn.execute(text("UPDATE model_jobs SET lease_until=now()-interval '1 second' WHERE id=:id"),old)
    new=await jobs.claim();assert new['generation']==old['generation']+1
    token=model_job_context.set(ModelJobContext(old['id'],old['generation']))
    try:
        with pytest.raises(asyncio.CancelledError):
            async with tenant_transaction(old['tenant_id'],old['principal_id']): pass
    finally:model_job_context.reset(token)
    await jobs.finish(old,status='succeeded',result={'stale':True})
    assert (await jobs.read(request_job))['status']=='running'
    await jobs.execute(new,{request_job['operation']:lambda payload:asyncio.sleep(0,result={'recovered':True})})
    assert (await jobs.read(request_job))['result']=={'recovered':True}


@pytest.mark.asyncio
async def test_lease_database_failure_stops_provider_without_fabricating_failure(request_job,monkeypatch):
    await jobs.submit(request_job);job=await jobs.claim()
    renew=jobs.renew;count=0;closed=asyncio.Event()
    async def unavailable(job):
        nonlocal count
        count+=1
        if count>1:raise ConnectionError('test database outage')
        return await renew(job)
    async def operation(_):
        try:await asyncio.Event().wait()
        finally:closed.set()
    monkeypatch.setattr(jobs,'renew',unavailable)
    with pytest.raises(ConnectionError,match='database outage'):
        await jobs.execute(job,{request_job['operation']:operation})
    assert closed.is_set()
    state=await jobs.read(request_job)
    assert state['status']=='running' and state['error'] is None


@pytest.mark.asyncio
async def test_real_worker_returns_persisted_result(request_job):
    if not os.getenv('CAD_MODEL_JOB_TEST_TEMPORAL'):pytest.skip('real Temporal requested separately')
    from temporalio.client import Client
    from app.workers.model_job_worker import ModelJobWorker
    from app.workflows.model_job import ModelJobWorkflow
    client=await Client.connect(os.environ['CAD_MODEL_JOB_TEST_TEMPORAL'],
        namespace=os.getenv('CAD_MODEL_JOB_TEST_TEMPORAL_NAMESPACE', 'default'))
    async def operation(payload):return {'observed_principal':str(usage_context.get().principal_id)}
    queue='model-success-'+str(uuid4())
    async with ModelJobWorker(client,task_queue=queue,workflows=[ModelJobWorkflow],
        activities=[jobs.submit_activity,jobs.read_activity,jobs.cancel_activity],
        model_operations={request_job['operation']:operation}):
        result=await client.execute_workflow(ModelJobWorkflow.run,request_job,id=queue,task_queue=queue)
    assert result=={'observed_principal':request_job['payload']['principal_id']}
    assert (await jobs.read(request_job))['result']==result


@pytest.mark.asyncio
async def test_terminal_run_closes_outstanding_steps(request_job):
    from uuid import UUID
    p=request_job['payload']
    async with tenant_transaction(UUID(p['tenant_id']),UUID(p['principal_id'])) as conn:
        run=UUID(p['workflow_run_id'])
        await transition_workflow(conn,run,expected=WorkflowStatus.PENDING,target=WorkflowStatus.PLANNING)
        step=await create_step(conn,tenant_id=UUID(p['tenant_id']),workflow_id=run,step_key='repair',step_index=1,kind='agent_repair')
        await transition_step(conn,step.step_id,expected=StepStatus.PENDING,target=StepStatus.READY)
        await transition_step(conn,step.step_id,expected=StepStatus.READY,target=StepStatus.RUNNING)
        await transition_workflow(conn,run,expected=WorkflowStatus.PLANNING,target=WorkflowStatus.FAILED,
            error_code='provider_failed',error_message='terminal failure')
        row=(await conn.execute(text('SELECT status,error_code,completed_at FROM step_runs WHERE id=:id'),{'id':step.step_id})).mappings().one()
        assert row['status']=='failed' and row['error_code']=='provider_failed' and row['completed_at']


@pytest.mark.asyncio
async def test_other_tenant_cannot_read_job(request_job):
    await jobs.submit(request_job)
    other=user_principal(str(uuid4()));await reconcile_principal(other)
    from sqlalchemy.exc import NoResultFound
    request={**request_job,'payload':{**request_job['payload'],'tenant_id':str(other.tenant_id),'principal_id':str(other.principal_id)}}
    with pytest.raises(NoResultFound):await jobs.read(request)


@pytest.mark.asyncio
async def test_provider_socket_cancellation_persists_real_usage_state(request_job,monkeypatch):
    from app.llm import create_llm_client
    from app.config import Settings
    monkeypatch.setattr(jobs,'HEARTBEAT_SECONDS',0.03)
    connected=asyncio.Event();disconnected=asyncio.Event()
    async def serve(reader,writer):
        try:
            headers=await reader.readuntil(b'\r\n\r\n')
            size=next(int(x.split(b':',1)[1]) for x in headers.split(b'\r\n') if x.lower().startswith(b'content-length:'))
            await reader.readexactly(size)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n')
            await writer.drain();connected.set()
            assert await reader.read()==b''
        finally:
            writer.close();await writer.wait_closed();disconnected.set()
    server=await asyncio.start_server(serve,'127.0.0.1',0)
    port=server.sockets[0].getsockname()[1]
    client=create_llm_client(Settings(_env_file=None,moonshot_api_key='test-only',llm_max_retries=0,
        llm_base_url=f'http://127.0.0.1:{port}/v1'), sink=llm_usage)
    async def operation(_):return await client.chat.completions.create(model='transport-test',messages=[],stream=True)
    await jobs.submit(request_job);job=await jobs.claim()
    task=asyncio.create_task(jobs.execute(job,{request_job['operation']:operation}))
    try:
        await asyncio.wait_for(connected.wait(),10)
        await jobs.cancel(request_job)
        await asyncio.wait_for(task,10)
        await asyncio.wait_for(disconnected.wait(),5)
        async with tenant_transaction(job['tenant_id'],job['principal_id']) as conn:
            calls=(await conn.execute(text('SELECT status,principal_id,total_tokens FROM llm_calls WHERE workflow_run_id=:id'),
                {'id':job['workflow_run_id']})).mappings().all()
        assert len(calls)==1 and calls[0]['status']=='cancelled'
        assert calls[0]['principal_id']==job['principal_id'] and calls[0]['total_tokens'] is None
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        await client.close();server.close();await server.wait_closed()


@pytest.mark.asyncio
async def test_real_temporal_wait_and_cancellation(request_job,monkeypatch):
    if not os.getenv('CAD_MODEL_JOB_TEST_TEMPORAL'):pytest.skip('real Temporal requested separately')
    from temporalio.client import Client, WorkflowFailureError
    from temporalio.worker import Worker
    from app.workflows.model_job import ModelJobWorkflow
    client=await Client.connect(os.environ['CAD_MODEL_JOB_TEST_TEMPORAL'],
        namespace=os.getenv('CAD_MODEL_JOB_TEST_TEMPORAL_NAMESPACE', 'default'))
    queue='model-job-test-'+str(uuid4())
    monkeypatch.setattr(jobs,'HEARTBEAT_SECONDS',0.05)
    started=asyncio.Event();closed=asyncio.Event();count=0
    async def operation(payload):
        nonlocal count
        count+=1;started.set()
        try: await asyncio.Event().wait()
        finally: closed.set()
    runner=asyncio.create_task(jobs.run_model_jobs({request_job['operation']:operation},concurrency=1))
    try:
        async with Worker(client,task_queue=queue,workflows=[ModelJobWorkflow],
                activities=[jobs.submit_activity,jobs.read_activity,jobs.cancel_activity]):
            handle=await client.start_workflow(ModelJobWorkflow.run,request_job,id='job-test-'+str(uuid4()),task_queue=queue)
            await asyncio.wait_for(started.wait(),15)
            # Optional 310s proves the old five-minute cutoff does not retry or fail.
            await asyncio.sleep(float(os.environ.get('CAD_MODEL_JOB_TEST_WAIT_SECONDS','0.2')))
            assert (await jobs.read(request_job))['status']=='running' and count==1
            await handle.cancel()
            with pytest.raises(WorkflowFailureError):await asyncio.wait_for(handle.result(),15)
            await asyncio.wait_for(closed.wait(),10)
            for _ in range(100):
                if (await jobs.read(request_job))['status']=='cancelled':break
                await asyncio.sleep(.05)
            assert (await jobs.read(request_job))['status']=='cancelled'
    finally:
        runner.cancel();await asyncio.gather(runner,return_exceptions=True)


@pytest.mark.asyncio
async def test_real_continue_as_new_keeps_one_job_and_provider_operation(request_job, monkeypatch):
    """Real Temporal/DB; only the poll timer is accelerated in this test worker."""
    if not os.getenv('CAD_MODEL_JOB_TEST_TEMPORAL'):
        pytest.skip('real Temporal required')
    from datetime import timedelta
    from temporalio import activity, workflow
    from temporalio.client import Client
    from app.workers.model_job_worker import ModelJobWorker
    from app.workflows.model_job import ModelJobWorkflow
    client = await Client.connect(os.environ['CAD_MODEL_JOB_TEST_TEMPORAL'])
    original_sleep = workflow.sleep
    async def short_poll(duration):
        return await original_sleep(timedelta(milliseconds=1))
    monkeypatch.setattr(workflow, 'sleep', short_poll)
    release = asyncio.Event()
    polls = calls = 0
    @activity.defn(name='model_jobs.read')
    async def counted_read(payload: dict) -> dict:
        nonlocal polls
        polls += 1
        if polls > 500:
            release.set()
        return await jobs.read(payload)
    async def operation(payload):
        nonlocal calls
        calls += 1
        await release.wait()
        return {'retained_workflow': payload['workflow_run_id']}
    queue = 'rollover-contract-' + str(uuid4())
    async with ModelJobWorker(client, task_queue=queue, workflows=[ModelJobWorkflow],
            activities=[jobs.submit_activity, counted_read, jobs.cancel_activity],
            model_operations={request_job['operation']: operation}):
        handle = await client.start_workflow(ModelJobWorkflow.run, request_job, id=queue, task_queue=queue)
        initial_run = handle.first_execution_run_id
        result = await handle.result()
        assert result == {'retained_workflow': request_job['payload']['workflow_run_id']}
        assert polls > 500 and calls == 1
        state = await jobs.read(request_job)
        assert state['status'] == 'succeeded'
        description = await client.get_workflow_handle(queue).describe()
        assert description.run_id != initial_run
        history = await client.get_workflow_handle(queue, run_id=initial_run).fetch_history()
        assert any(event.HasField('workflow_execution_continued_as_new_event_attributes') for event in history.events)
