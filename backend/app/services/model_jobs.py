"""Durable model execution independent of Temporal activity wall clocks.

Only short enqueue/read/cancel operations run as Temporal activities. A separate
worker loop owns provider calls with no elapsed-time deadline. Leases detect dead
workers; they are renewed while a healthy call is still waiting for data.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
from uuid import UUID

from sqlalchemy import text
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.db import get_database_engine, tenant_transaction
from app.execution.canonical import canonical_sha256
from app.model_job_context import ModelJobContext, model_job_context
from app.services.llm_usage import UsageContext, usage_context
from app.contracts.model_operations import DURABLE_OPERATIONS, ModelHandlers

logger = logging.getLogger(__name__)
LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 3


@asynccontextmanager
async def dispatcher():
    async with get_database_engine().begin() as conn:
        await conn.execute(text('SET LOCAL ROLE cad_agent_model_dispatch'))
        yield conn


async def submit(request: dict) -> dict:
    payload = request['payload']
    operation = request['operation']
    if operation not in DURABLE_OPERATIONS:
        raise ApplicationError('Unsupported model operation', type='model_job_operation_invalid', non_retryable=True)
    digest = canonical_sha256({'operation': operation, 'payload': payload})
    values = dict(id=UUID(request['job_id']), tenant=UUID(payload['tenant_id']),
                  principal=UUID(payload['principal_id']), run=UUID(payload['workflow_run_id']),
                  operation=operation, payload=json.dumps(payload, allow_nan=False), digest=digest)
    async with tenant_transaction(values['tenant'], values['principal']) as conn:
        owner = (await conn.execute(text('SELECT requested_by_principal_id,status,cancellation_requested_at '
            'FROM workflow_runs WHERE id=:run FOR UPDATE'), values)).mappings().one()
        if str(owner['requested_by_principal_id']) != str(values['principal']):
            raise PermissionError('Model job principal differs from task submitter')
        if owner['status'] in {'failed','cancelled','timed_out','succeeded','cancelling'} or owner['cancellation_requested_at']:
            return {'status': 'cancelled'}
        await conn.execute(text("""INSERT INTO model_jobs
            (id,tenant_id,principal_id,workflow_run_id,operation,payload,payload_hash)
            VALUES(:id,:tenant,:principal,:run,:operation,CAST(:payload AS json),:digest)
            ON CONFLICT(id) DO NOTHING"""), values)
        row = (await conn.execute(text('SELECT * FROM model_jobs WHERE id=:id'), values)).mappings().one()
        if row['payload_hash'] != digest or row['workflow_run_id'] != values['run']:
            raise ApplicationError('Model job input changed on replay', type='model_job_input_conflict', non_retryable=True)
        return {'status': row['status']}


async def read(request: dict) -> dict:
    p = request['payload']
    async with tenant_transaction(UUID(p['tenant_id']), UUID(p['principal_id'])) as conn:
        row = (await conn.execute(text('SELECT status,result,error FROM model_jobs WHERE id=:id '
            'AND workflow_run_id=:run AND principal_id=:principal'),
            dict(id=UUID(request['job_id']), run=UUID(p['workflow_run_id']), principal=UUID(p['principal_id'])))).mappings().one()
        return dict(row)


async def cancel(request: dict) -> dict:
    p = request['payload']
    async with tenant_transaction(UUID(p['tenant_id']), UUID(p['principal_id'])) as conn:
        await conn.execute(text("""UPDATE model_jobs SET cancel_requested=true,
            status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
            completed_at=CASE WHEN status='queued' THEN now() ELSE completed_at END
            WHERE id=:id AND workflow_run_id=:run AND principal_id=:principal
              AND status IN ('queued','running')"""),
            dict(id=UUID(request['job_id']), run=UUID(p['workflow_run_id']), principal=UUID(p['principal_id'])))
    return {'status': 'cancellation_requested'}


async def claim() -> dict | None:
    async with dispatcher() as conn:
        row = (await conn.execute(text("""WITH next AS (
            SELECT id FROM model_jobs WHERE status='queued'
               OR (status='running' AND lease_until < now())
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
        ) UPDATE model_jobs j SET status='running',generation=generation+1,
            lease_until=now()+(:lease * interval '1 second')
            FROM next WHERE j.id=next.id RETURNING j.*"""), {'lease': LEASE_SECONDS})).mappings().one_or_none()
        return dict(row) if row else None


async def renew(job: dict) -> bool:
    async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
        return bool(await conn.scalar(text("""UPDATE model_jobs j
            SET lease_until=now()+(:lease * interval '1 second')
            WHERE j.id=:id AND j.generation=:generation AND j.status='running' AND NOT j.cancel_requested
              AND EXISTS(SELECT 1 FROM workflow_runs w WHERE w.id=j.workflow_run_id
                AND w.status NOT IN ('failed','cancelled','timed_out','succeeded','cancelling')
                AND w.cancellation_requested_at IS NULL)
            RETURNING j.id"""), {**job, 'lease': LEASE_SECONDS}))


async def finish(job: dict, *, status: str, result=None, error=None):
    async with tenant_transaction(job['tenant_id'], job['principal_id']) as conn:
        await conn.execute(text("""UPDATE model_jobs SET
            status=CASE WHEN cancel_requested THEN 'cancelled' ELSE :status END,
            result=CASE WHEN cancel_requested THEN NULL ELSE CAST(:result AS json) END,
            error=CASE WHEN cancel_requested THEN NULL ELSE CAST(:error AS json) END,
            lease_until=NULL,completed_at=now()
            WHERE id=:id AND generation=:generation AND status='running'"""),
            {**job, 'status': status, 'result': json.dumps(result, allow_nan=False),
             'error': json.dumps(error, allow_nan=False)})


async def execute(job: dict, operations: ModelHandlers):
    """Run one fenced job. Cancellation closes the actual provider connection."""
    async def invoke():
        context = model_job_context.set(ModelJobContext(job['id'], job['generation']))
        usage = usage_context.set(UsageContext(job['tenant_id'],job['principal_id'],
            job['workflow_run_id'],job['operation'],job['generation']))
        try:
            if canonical_sha256({'operation':job['operation'],'payload':job['payload']}) != job['payload_hash']:
                raise ValueError('Model job payload integrity mismatch')
            return await operations[job['operation']](job['payload'])
        finally:
            usage_context.reset(usage)
            model_job_context.reset(context)

    async def keep_lease():
        while True:
            if not await renew(job):
                return
            await asyncio.sleep(HEARTBEAT_SECONDS)

    if not await renew(job):
        await finish(job,status='cancelled')
        return
    work = asyncio.create_task(invoke())
    lease = asyncio.create_task(keep_lease())
    recovering = False
    try:
        done, _ = await asyncio.wait({work,lease},return_when=asyncio.FIRST_COMPLETED)
        if lease in done:
            # A lost lease or cancellation must stop the call before any retry.
            work.cancel()
            await asyncio.gather(work,return_exceptions=True)
            recovering = True
            lease.result()  # Preserve a real database failure for recovery.
            await finish(job,status='cancelled')
            return
        result = await work
        recovering = True
        await finish(job,status='succeeded',result=result)
    except asyncio.CancelledError:
        work.cancel()
        await asyncio.gather(work,return_exceptions=True)
        # Worker shutdown: release for recovery; user cancellation stays terminal.
        async with tenant_transaction(job['tenant_id'],job['principal_id']) as conn:
            await conn.execute(text("""UPDATE model_jobs SET
                status=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'queued' END,lease_until=NULL
                WHERE id=:id AND generation=:generation AND status='running'"""),job)
        raise
    except Exception as exc:
        if recovering:
            # Losing database connectivity is not a failed model response.
            # Leave the fenced lease for takeover once the database recovers.
            raise
        error = {'code': getattr(exc,'type',None) or type(exc).__name__,
                 'message': str(exc)[:4000], 'details': list(exc.details) if isinstance(exc,ApplicationError) else []}
        await finish(job,status='failed',error=error)
    finally:
        work.cancel()
        lease.cancel()
        await asyncio.gather(work,lease,return_exceptions=True)


async def run_model_jobs(operations: ModelHandlers, *, concurrency: int = 4):
    running = set()
    try:
        while True:
            for task in tuple(running):
                if task.done():
                    running.remove(task)
                    if not task.cancelled() and task.exception():
                        exc = task.exception()
                        logger.error('Model job worker failed; expired lease will recover',
                                     exc_info=(type(exc),exc,exc.__traceback__))
            while len(running) < concurrency:
                job = await claim()
                if job is None:
                    break
                running.add(asyncio.create_task(execute(job,operations)))
            await asyncio.sleep(0.5)
    finally:
        for task in running:
            task.cancel()
        await asyncio.gather(*running,return_exceptions=True)


@activity.defn(name='model_jobs.submit')
async def submit_activity(request: dict) -> dict:
    return await submit(request)


@activity.defn(name='model_jobs.read')
async def read_activity(request: dict) -> dict:
    return await read(request)


@activity.defn(name='model_jobs.cancel')
async def cancel_activity(request: dict) -> dict:
    return await cancel(request)
