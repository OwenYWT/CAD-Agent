"""Same-connection execution fence; lock parent before model job.

This has no dependency on transaction construction. The default transaction
wrapper continues to apply it so an unmigrated consumer cannot bypass it.
"""
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from app.model_job_context import ModelJobContext


async def assert_model_execution_current(connection: AsyncConnection, job: ModelJobContext) -> None:
    active = await connection.scalar(text("""SELECT id FROM workflow_runs
        WHERE id=(SELECT workflow_run_id FROM model_jobs WHERE id=:job)
          AND status NOT IN ('failed','cancelled','timed_out','succeeded','cancelling')
          AND cancellation_requested_at IS NULL FOR UPDATE"""), {'job': job.job_id})
    lease = await connection.scalar(text("""SELECT id FROM model_jobs
        WHERE id=:job AND generation=:generation AND status='running'
          AND NOT cancel_requested AND lease_until>now() FOR UPDATE"""),
        {'job': job.job_id, 'generation': job.generation})
    if not active or not lease:
        raise asyncio.CancelledError('Model job cancelled or lease superseded')
