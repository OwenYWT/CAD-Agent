"""At-least-once delivery of immutable DB intents to idempotent Temporal IDs.

The dispatcher can read only its dispatch table across tenants. CAD operations
continue to authorize their principal and revision in the existing workflow.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
from uuid import UUID, uuid4

from sqlalchemy import text
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.db import get_database_engine, tenant_transaction
from app.execution.canonical import canonical_sha256
from app.services.run_state import IdempotencyConflict

logger = logging.getLogger(__name__)


async def persist_dispatch(connection, *, tenant_id: UUID, principal_id: UUID,
                           workflow_id: UUID, workflow_type: str, temporal_id: str,
                           task_queue: str, payload: dict):
    if str(payload.get("tenant_id")) != str(tenant_id) or str(payload.get("workflow_run_id")) != str(workflow_id):
        raise ValueError("dispatch payload does not match its durable owner")
    digest = canonical_sha256(payload)
    await connection.execute(text("""INSERT INTO workflow_dispatches(
        workflow_id,tenant_id,principal_id,workflow_type,temporal_id,task_queue,payload,payload_hash)
        VALUES(:id,:tenant,:principal,:kind,:temporal,:queue,CAST(:payload AS jsonb),:hash)
        ON CONFLICT(workflow_id) DO NOTHING"""),
        {"id":workflow_id,"tenant":tenant_id,"principal":principal_id,"kind":workflow_type,
         "temporal":temporal_id,"queue":task_queue,"payload":json.dumps(payload),"hash":digest})
    row = (await connection.execute(text("SELECT * FROM workflow_dispatches WHERE workflow_id=:id"),
                                    {"id":workflow_id})).mappings().one()
    if (row["payload_hash"],row["temporal_id"],row["workflow_type"],row["task_queue"]) != (digest,temporal_id,workflow_type,task_queue):
        raise IdempotencyConflict("workflow dispatch was retried with different input")
    return dict(row)


async def start_dispatch(client, dispatch: dict):
    if canonical_sha256(dispatch["payload"]) != dispatch["payload_hash"]:
        raise ValueError("dispatch payload integrity check failed")
    try:
        return await client.start_workflow(
            dispatch["workflow_type"], dispatch["payload"], id=dispatch["temporal_id"],
            task_queue=dispatch["task_queue"],
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
    except WorkflowAlreadyStartedError:
        return client.get_workflow_handle(dispatch["temporal_id"])


async def acknowledge_dispatch(dispatch: dict):
    async with tenant_transaction(dispatch["tenant_id"], dispatch["principal_id"]) as conn:
        await conn.execute(text("""UPDATE workflow_dispatches SET status='dispatched',
            dispatched_at=COALESCE(dispatched_at,CURRENT_TIMESTAMP),lease_until=NULL,lease_token=NULL,last_error_code=NULL
            WHERE workflow_id=:id"""), {"id":dispatch["workflow_id"]})


@asynccontextmanager
async def _dispatcher_transaction():
    async with get_database_engine().begin() as conn:
        await conn.execute(text("SET LOCAL ROLE cad_agent_dispatcher"))
        yield conn


async def claim_dispatch(*, task_queues: tuple[str, ...], lease_seconds=30):
    token = uuid4()
    async with _dispatcher_transaction() as conn:
        row = (await conn.execute(text("""UPDATE workflow_dispatches SET status='dispatching',
            attempts=attempts+1,lease_token=:token,lease_until=CURRENT_TIMESTAMP+make_interval(secs=>:ttl)
            WHERE workflow_id=(SELECT workflow_id FROM workflow_dispatches
                WHERE task_queue=ANY(:queues) AND status<>'dispatched'
                  AND next_attempt_at<=CURRENT_TIMESTAMP
                  AND (lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP)
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING *"""), {"token":token,"ttl":lease_seconds,"queues":list(task_queues)})).mappings().one_or_none()
        return dict(row) if row else None


async def finish_claim(dispatch: dict, *, error_code: str | None = None):
    async with _dispatcher_transaction() as conn:
        if error_code is None:
            await conn.execute(text("""UPDATE workflow_dispatches SET status='dispatched',
                dispatched_at=COALESCE(dispatched_at,CURRENT_TIMESTAMP),lease_token=NULL,lease_until=NULL,last_error_code=NULL
                WHERE workflow_id=:id AND status='dispatching' AND lease_token=:token"""),
                {"id":dispatch["workflow_id"],"token":dispatch["lease_token"]})
        else:
            await conn.execute(text("""UPDATE workflow_dispatches SET status='pending',
                lease_token=NULL,lease_until=NULL,last_error_code=:error,
                next_attempt_at=CURRENT_TIMESTAMP+make_interval(secs=>:delay)
                WHERE workflow_id=:id AND status='dispatching' AND lease_token=:token"""),
                {"id":dispatch["workflow_id"],"token":dispatch["lease_token"],"error":error_code[:200],
                 "delay":min(60,2 ** min(dispatch["attempts"],6))})


async def dispatch_once(client, *, task_queues: tuple[str, ...]) -> bool:
    row = await claim_dispatch(task_queues=task_queues)
    if row is None:
        return False
    try:
        await asyncio.wait_for(start_dispatch(client, row), timeout=20)
    except asyncio.CancelledError:
        # A replacement process may reclaim the expiring lease. Never mark a
        # shutdown as successful delivery or erase the durable input.
        raise
    except Exception as exc:
        await finish_claim(row, error_code=type(exc).__name__)
        logger.warning("Workflow dispatch %s will retry (%s)",row["workflow_id"],type(exc).__name__)
    else:
        await finish_claim(row)
    return True


async def run_dispatcher(client, *, task_queues: tuple[str, ...]):
    while True:
        try:
            worked = await dispatch_once(client, task_queues=task_queues)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Workflow dispatch scan unavailable (%s)",type(exc).__name__)
            worked = False
        await asyncio.sleep(0.1 if worked else 1)


async def dispatcher_readiness():
    """Fail startup if migrations or the worker's narrow dispatcher grant are missing."""
    async with _dispatcher_transaction() as conn:
        await conn.execute(text("SELECT workflow_id FROM workflow_dispatches LIMIT 0"))
        await conn.execute(text("UPDATE workflow_dispatches SET status=status WHERE false"))
