import asyncio
import logging
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import close_database, database_readiness
from app.execution.composition import get_execution_backend
from app.object_store import object_store_readiness
from app.temporal_client import temporal_readiness

# Configure logging with request-id correlation
from app.logging_context import RequestIdFilter

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] [req=%(request_id)s] %(message)s",
    datefmt="%H:%M:%S",
)
# Attach the filter to all root handlers so every record gets a request_id attr.
for _h in logging.getLogger().handlers:
    _h.addFilter(RequestIdFilter())

from app.api.generate import router as generate_router
from app.api.execute import router as execute_router
from app.api.files import router as files_router
from app.api.parts import router as parts_router
from app.api.batch import router as batch_router
from app.api.history import router as history_router
from app.api.analyze import router as analyze_router
from app.api.dfm_rules import router as dfm_rules_router
from app.api.knowledge import router as knowledge_router
from app.api.feedback import router as feedback_router
from app.api.login import router as login_router
from app.api.capabilities import router as capabilities_router
from app.api.capability_actions import router as capability_actions_router
from app.api.onshape import router as onshape_router
from app.api.agent_tools import router as agent_tools_router
from app.api.tasks import (
    durable_task_websocket,
    router as durable_tasks_router,
)
from app.api.changes import router as changes_router
from app.api.revisions import router as revisions_router
from app.api.websocket import websocket_endpoint
from app.fusion360.api import router as fusion360_router
from app.fusion360.agent_api import router as fusion360_agent_router

logger = logging.getLogger(__name__)

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _cleanup_old_files():
    """Remove generated files older than file_ttl_hours."""
    storage = Path(settings.file_storage_dir)
    if not storage.exists():
        return
    cutoff = time.time() - settings.file_ttl_hours * 3600
    count = 0
    for d in storage.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            count += 1
    if count:
        logger.info(f"Cleaned up {count} old file directories")


def _startup_self_check(execution_backend=None):
    """Report missing LLM or MCAD execution dependencies before the first request."""
    problems = []

    if not settings.has_llm_credentials:
        problems.append(
            f"{settings.llm_credentials_error} — planner/codegen 的第一次 LLM 调用会失败。"
            "请在 backend/.env 写入对应的模型 API key。"
        )

    try:
        backend = execution_backend or get_execution_backend()
        backend.runtime_snapshot()
    except Exception as exc:
        problems.append(f"MCAD ExecutionBackend 不可用: {str(exc)[:300]}")

    if problems:
        logger.error(
            "启动自检发现 %d 个阻断问题，生成功能将不可用:\n  - %s",
            len(problems), "\n  - ".join(problems),
        )
    else:
        logger.info("启动自检通过: LLM key + MCAD ExecutionBackend 就绪")
    return problems


async def _periodic_cleanup(interval_s: int = 3600):
    """Run file cleanup on a loop, not just once at startup (disk would otherwise fill)."""
    import asyncio
    while True:
        try:
            await asyncio.sleep(interval_s)
            _cleanup_old_files()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Periodic cleanup error: {e}")


async def _durable_control_plane_readiness() -> dict:
    if not settings.durable_control_plane_enabled:
        return {
            "status": "disabled",
            "dependencies": {},
            "problems": [],
        }

    probes = {
        "postgresql": database_readiness,
        "object_store": object_store_readiness,
        "temporal": temporal_readiness,
    }
    results = await asyncio.gather(
        *(probe() for probe in probes.values()),
        return_exceptions=True,
    )
    dependencies = {}
    problems = []
    for name, result in zip(probes, results, strict=True):
        if isinstance(result, BaseException):
            dependencies[name] = {
                "status": "unavailable",
                "error_type": type(result).__name__,
            }
            # Provider exception messages can include connection credentials.
            problems.append(f"{name} unavailable ({type(result).__name__})")
        else:
            dependencies[name] = result
    return {
        "status": "degraded" if problems else "ready",
        "dependencies": dependencies,
        "problems": problems,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    # Refuse to boot with an unsafe auth config (empty/placeholder token secret,
    # dev code exposure on). This is a hard gate, not a warning.
    settings.assert_auth_config_safe()
    settings.assert_sandbox_config_safe()
    settings.assert_durable_control_plane_config_safe()
    _cleanup_old_files()
    app.state.execution_backend = get_execution_backend()
    app.state.startup_problems = _startup_self_check(app.state.execution_backend)
    # Provision admin + default invite eagerly so misconfig surfaces at boot, not
    # on the first request. Both are no-ops when their config is unset/empty.
    from app.storage.auth import ensure_admin_user, ensure_default_invite_code
    await ensure_admin_user()
    await ensure_default_invite_code()
    from app.storage import local_runs
    await local_runs.initialize()
    reconciled_runs = await local_runs.reconcile_incomplete_runs()
    app.state.reconciled_local_workflow_runs = reconciled_runs
    if reconciled_runs:
        logger.warning(
            "Reconciled %d interrupted process-local workflow run(s) as non-resumable",
            len(reconciled_runs),
        )
    from app.agent.recovery import recover_running_runs
    await recover_running_runs()
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    yield
    # Shutdown
    cleanup_task.cancel()
    from app.workflows.local import get_local_workflow_manager
    await get_local_workflow_manager().shutdown()
    from app.storage.history import close_db
    await close_db()
    from app.storage.auth import close_db as close_auth_db
    await close_auth_db()
    from app.dfm.rule_store import close_db as close_rules_db
    await close_rules_db()
    from app.dfm.knowledge_graph import close_db as close_kg_db
    await close_kg_db()
    await close_database()


def create_app() -> FastAPI:
    app = FastAPI(
        title="CAD Agent Web API",
        description="Web application for generating and previewing CAD files from natural language.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )

    # REST API
    app.include_router(generate_router)
    app.include_router(execute_router)
    app.include_router(files_router, prefix="/api")
    app.include_router(parts_router)
    app.include_router(batch_router)
    app.include_router(history_router)
    app.include_router(analyze_router, prefix="/api")
    app.include_router(dfm_rules_router, prefix="/api")
    app.include_router(knowledge_router, prefix="/api")
    app.include_router(feedback_router)
    app.include_router(login_router)
    app.include_router(capabilities_router)
    app.include_router(capability_actions_router)
    app.include_router(fusion360_router)
    app.include_router(fusion360_agent_router)
    app.include_router(onshape_router)
    app.include_router(agent_tools_router)
    app.include_router(durable_tasks_router)
    app.include_router(changes_router)
    app.include_router(revisions_router)

    # WebSocket
    app.websocket("/ws/{session_id}")(websocket_endpoint)
    app.websocket("/ws/tasks/{workflow_run_id}")(durable_task_websocket)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        from fastapi.responses import JSONResponse

        durable = await _durable_control_plane_readiness()
        problems = [
            *getattr(app.state, "startup_problems", []),
            *durable["problems"],
        ]
        if problems:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "degraded",
                    "problems": problems,
                    "durable_control_plane": durable,
                },
            )
        return {
            "status": "ready",
            "problems": [],
            "durable_control_plane": durable,
        }

    if FRONTEND_DIST.exists():
        assets_dir = FRONTEND_DIST / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def serve_web_app(full_path: str):
            # Never disguise an unknown API/WebSocket path as a successful SPA page.
            # Apart from confusing clients, returning index.html with 200 makes path
            # traversal probes appear to succeed after URL normalization.
            if full_path == "api" or full_path.startswith(("api/", "ws/")):
                from fastapi import HTTPException

                raise HTTPException(status_code=404, detail="Not found")
            requested = (FRONTEND_DIST / full_path).resolve()
            dist_root = FRONTEND_DIST.resolve()
            if requested.is_file() and str(requested).startswith(str(dist_root)):
                return FileResponse(requested)
            return FileResponse(FRONTEND_DIST / "index.html")

    return app


app = create_app()
