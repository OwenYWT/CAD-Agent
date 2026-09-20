"""Independent, administrator-only usage application; not mounted in the CAD UI."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.auth import get_current_user
from app.api.login import login_with_password
from app.config import settings
from app.db import close_database, database_readiness, get_database_engine
from app.domain.identity import user_principal


async def require_monitor_admin(user=Depends(get_current_user)):
    if not user.get("is_admin"):
        raise HTTPException(403, "仅平台管理员可查看账号使用情况")
    return user


@asynccontextmanager
async def lifespan(app):
    settings.assert_auth_config_safe()
    if not settings.durable_control_plane_enabled:
        raise RuntimeError("Monitoring requires the durable PostgreSQL control plane")
    await database_readiness()
    yield
    await close_database()


app = FastAPI(title="CAD Agent 使用监控", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_api_route("/api/auth/login/password", login_with_password, methods=["POST"])
assets = Path(__file__).with_name("monitor_assets")
app.mount("/assets", StaticFiles(directory=assets, check_dir=False), name="assets")


@app.middleware("http")
async def private_responses(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


@app.get("/")
async def index():
    return FileResponse(assets / "index.html")


@app.get("/health")
async def health():
    await database_readiness()
    async with reader() as conn:
        await conn.execute(text("SELECT id FROM llm_calls LIMIT 0"))
    return {"status": "ready"}


@asynccontextmanager
async def reader():
    async with get_database_engine().begin() as conn:
        await conn.execute(text("SET LOCAL ROLE cad_agent_monitor"))
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        await conn.execute(text("SET LOCAL statement_timeout = '10s'"))
        yield conn


async def window(start: datetime | None = None, end: datetime | None = None):
    if end is None:
        # Records use the database clock; host/VM clock skew must not hide new calls.
        async with reader() as conn:
            end = await conn.scalar(text("SELECT CURRENT_TIMESTAMP"))
    start = start or end - timedelta(days=7)
    if start.tzinfo is None or end.tzinfo is None or start >= end or end - start > timedelta(days=366):
        raise HTTPException(422, "时间必须包含时区，结束时间晚于开始时间，范围不超过 366 天")
    return dict(start=start, end=end)


@app.get("/api/monitor/accounts", dependencies=[Depends(require_monitor_admin)])
async def accounts(period=Depends(window)):
    async with reader() as conn:
        rows = (await conn.execute(text("""
            WITH tasks AS (
                SELECT p.external_subject AS account,
                    count(*) AS tasks,
                    count(*) FILTER(WHERE w.status='succeeded') AS succeeded,
                    count(*) FILTER(WHERE w.status IN ('failed','timed_out')) AS failed,
                    count(*) FILTER(WHERE w.status='cancelled') AS cancelled,
                    count(*) FILTER(WHERE w.status NOT IN ('succeeded','failed','timed_out','cancelled')) AS active,
                    max(w.created_at) AS last_task_at
                FROM workflow_runs w JOIN principals p ON p.id=w.requested_by_principal_id
                WHERE w.created_at>=:start AND w.created_at<:end GROUP BY p.external_subject
            ), calls AS (
                SELECT p.external_subject AS account, count(*) AS calls,
                    count(*) FILTER(WHERE c.status='failed') AS call_failures,
                    count(*) FILTER(WHERE c.status='truncated') AS truncated,
                    count(*) FILTER(WHERE c.status='started') AS unfinished_calls,
                    count(*) FILTER(WHERE c.total_tokens IS NULL) AS unknown_usage_calls,
                    sum(c.prompt_tokens) AS prompt_tokens, sum(c.completion_tokens) AS completion_tokens,
                    sum(c.total_tokens) AS total_tokens, sum(c.reasoning_tokens) AS reasoning_tokens,
                    round(avg(c.duration_ms)) AS avg_duration_ms,
                    max(c.started_at) AS last_call_at
                FROM llm_calls c JOIN principals p ON p.id=c.principal_id
                WHERE c.started_at>=:start AND c.started_at<:end GROUP BY p.external_subject
            ), people AS (
                SELECT external_subject AS account,max(display_name) AS name, count(*) AS workspaces,
                    jsonb_object_agg(id::text,display_name) AS principal_names
                FROM principals WHERE kind IN ('user','service') GROUP BY external_subject
            )
            SELECT p.*,coalesce(t.tasks,0) AS tasks,coalesce(t.succeeded,0) AS succeeded,
                coalesce(t.failed,0) AS failed,coalesce(t.cancelled,0) AS cancelled,coalesce(t.active,0) AS active,
                coalesce(c.calls,0) AS calls,coalesce(c.call_failures,0) AS call_failures,
                coalesce(c.truncated,0) AS truncated,coalesce(c.unfinished_calls,0) AS unfinished_calls,
                coalesce(c.unknown_usage_calls,0) AS unknown_usage_calls,
                c.prompt_tokens,c.completion_tokens,c.total_tokens,c.reasoning_tokens,c.avg_duration_ms,
                greatest(t.last_task_at,c.last_call_at) AS last_used_at
            FROM people p LEFT JOIN tasks t USING(account) LEFT JOIN calls c USING(account)
            ORDER BY last_used_at DESC NULLS LAST,p.account
        """), period)).mappings().all()
        coverage = (await conn.execute(text("SELECT min(started_at) AS first_recorded_call FROM llm_calls"))).mappings().one()
    accounts = []
    for row in rows:
        item = dict(row)
        names = item.pop("principal_names")
        if item["account"].startswith("user:"):
            # Shared-workspace aliases must not replace the login account label.
            canonical = user_principal(item["account"][len("user:"):])
            item["name"] = names.get(str(canonical.principal_id)) or item["name"]
        accounts.append(item)
    return {"accounts": accounts, **period, **dict(coverage)}


@app.get("/api/monitor/tasks", dependencies=[Depends(require_monitor_admin)])
async def tasks(account: str | None = None, offset: int = Query(0, ge=0),
                limit: int = Query(30, ge=1, le=100), period=Depends(window)):
    params = {**period, "account": account, "offset": offset, "limit": limit}
    async with reader() as conn:
        rows = (await conn.execute(text("""SELECT w.id,w.kind,w.status,w.error_code,
            w.created_at,w.started_at,w.completed_at,p.external_subject AS account,p.display_name AS name,
            count(*) OVER() AS total_rows
            FROM workflow_runs w JOIN principals p ON p.id=w.requested_by_principal_id
            WHERE w.created_at>=:start AND w.created_at<:end
                AND (CAST(:account AS text) IS NULL OR p.external_subject=:account)
            ORDER BY w.created_at DESC,w.id LIMIT :limit OFFSET :offset"""), params)).mappings().all()
    return {"items": [dict(r) for r in rows], "offset": offset}


@app.get("/api/monitor/calls", dependencies=[Depends(require_monitor_admin)])
async def calls(account: str | None = None, offset: int = Query(0, ge=0),
                limit: int = Query(30, ge=1, le=100), period=Depends(window)):
    params = {**period, "account": account, "offset": offset, "limit": limit}
    async with reader() as conn:
        rows = (await conn.execute(text("""SELECT c.*,p.external_subject AS account,p.display_name AS name,
            count(*) OVER() AS total_rows FROM llm_calls c JOIN principals p ON p.id=c.principal_id
            WHERE c.started_at>=:start AND c.started_at<:end
                AND (CAST(:account AS text) IS NULL OR p.external_subject=:account)
            ORDER BY c.started_at DESC,c.id LIMIT :limit OFFSET :offset"""), params)).mappings().all()
    return {"items": [dict(r) for r in rows], "offset": offset}
