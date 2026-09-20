"""Persist provider facts, including retries and failures, without prompt contents."""
from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import text

from app.config import settings
from app.db import tenant_transaction
from app.principal_context import current_principal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageContext:
    tenant_id: UUID
    principal_id: UUID
    workflow_run_id: UUID | None = None
    activity_type: str | None = None
    activity_attempt: int | None = None


usage_context: ContextVar[UsageContext | None] = ContextVar("llm_usage_context", default=None)


async def start_call(*, provider: str, model: str, request_hash: str, attempt: int):
    if not settings.durable_control_plane_enabled:
        return None
    context = usage_context.get()
    if context is None:
        principal = current_principal()
        # Never silently attribute a background call to an anonymous account.
        if principal.kind.value == "local_anonymous":
            logger.error("LLM_USAGE_UNATTRIBUTED: missing authenticated principal")
            return None
        context = UsageContext(principal.tenant_id, principal.principal_id)
    call_id = uuid4()
    try:
        async with tenant_transaction(context.tenant_id, context.principal_id) as conn:
            await conn.execute(text("""INSERT INTO llm_calls
                (id,tenant_id,principal_id,workflow_run_id,activity_type,activity_attempt,
                 provider,model,request_hash,attempt,status)
                VALUES(:id,:tenant,:principal,:workflow,:activity,:activity_attempt,
                       :provider,:model,:request_hash,:attempt,'started')"""),
                dict(id=call_id, tenant=context.tenant_id, principal=context.principal_id,
                     workflow=context.workflow_run_id, activity=context.activity_type,
                     activity_attempt=context.activity_attempt, provider=provider,
                     model=model, request_hash=request_hash, attempt=attempt))
        return call_id, context
    except Exception:
        # A telemetry outage must not overwrite a valid CAD operation's result.
        logger.exception("LLM_USAGE_WRITE_FAILED: start call %s", call_id)
        return None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


async def finish_call(handle, *, duration_ms: int, provenance=None, error=None):
    if handle is None:
        return
    call_id, context = handle
    provenance = provenance or {}
    usage = provenance.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    finish = provenance.get("finish_reason")
    import asyncio
    status = ("cancelled" if isinstance(error, asyncio.CancelledError) else "failed") if error else (
        "truncated" if finish == "length" else "succeeded")
    try:
        async with tenant_transaction(context.tenant_id, context.principal_id, fence_model_job=False) as conn:
            await conn.execute(text("""UPDATE llm_calls SET
                status=:status,completed_at=CURRENT_TIMESTAMP,duration_ms=:duration,
                provider_response_id=:response_id,response_hash=:response_hash,finish_reason=:finish,
                model=COALESCE(:model,model),
                prompt_tokens=:prompt,completion_tokens=:completion,total_tokens=:total,
                reasoning_tokens=:reasoning,error_code=:error,http_status=:http_status
                WHERE id=:id AND status='started'"""),
                dict(id=call_id, status=status, duration=duration_ms,
                     response_id=provenance.get("provider_response_id"), model=provenance.get("model"),
                     response_hash=provenance.get("response_hash"), finish=finish,
                     prompt=_count(usage.get("prompt_tokens")), completion=_count(usage.get("completion_tokens")),
                     total=_count(usage.get("total_tokens")), reasoning=_count(details.get("reasoning_tokens")),
                     error=type(error).__name__ if error else None,
                     http_status=getattr(error, "status_code", None)))
    except Exception:
        logger.exception("LLM_USAGE_WRITE_FAILED: finish call %s", call_id)
