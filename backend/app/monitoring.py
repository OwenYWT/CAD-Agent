"""Independent, administrator-only usage application; not mounted in the CAD UI."""
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.auth import get_current_user
from app.services.authentication import login_with_password, AuthenticationError
from app.models.authentication import LoginWithPasswordRequest
from app.config import settings
from app.db import close_database, database_readiness
from app.services import monitor_queries
from app.services.monitor_queries import reader


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
    await monitor_queries.close_monitor_database()
    await close_database()


app = FastAPI(title="CAD Agent 使用监控", lifespan=lifespan, docs_url=None, redoc_url=None)
@app.post("/api/auth/login/password")
async def monitor_login(body: LoginWithPasswordRequest, request: Request):
    try:
        return await login_with_password(body.phone, body.password, request.client.host if request.client else "unknown")
    except AuthenticationError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc

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




async def window(start: datetime | None = None, end: datetime | None = None):
    try:
        return await monitor_queries.window(start, end)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/monitor/accounts", dependencies=[Depends(require_monitor_admin)])
async def accounts(period=Depends(window)):
    return await monitor_queries.accounts(period)


@app.get("/api/monitor/tasks", dependencies=[Depends(require_monitor_admin)])
async def tasks(account: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100), period=Depends(window)):
    return await monitor_queries.tasks(account, offset, limit, period=period)


@app.get("/api/monitor/calls", dependencies=[Depends(require_monitor_admin)])
async def calls(account: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100), period=Depends(window)):
    return await monitor_queries.calls(account, offset, limit, period=period)
