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
from app.api.onshape import router as onshape_router
from app.api.websocket import websocket_endpoint

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


def _startup_self_check():
    """Fail loud at boot if the core can't actually generate, instead of dying on the
    first tester prompt. Checks the three things the audit found missing on a fresh host:
    LLM key, Docker daemon, sandbox image."""
    problems = []

    if not settings.has_llm_credentials:
        problems.append(
            "DASHSCOPE_API_KEY 未设置 — planner/codegen 的第一次 LLM 调用会失败。"
            "请在 backend/.env 写入 dashscope_api_key。"
        )

    if settings.sandbox_runtime.strip().lower() == "podman":
        import subprocess
        command = settings.sandbox_command or "podman"
        result = subprocess.run(
            [command, "image", "exists", settings.sandbox_image],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            problems.append(
                f"Sandbox image '{settings.sandbox_image}' not found for Podman. "
                "Run: cd backend/sandbox && podman build -t cad-agent-sandbox:latest ."
            )
    else:
        try:
            import docker
            try:
                client = docker.from_env()
                client.ping()
                try:
                    client.images.get(settings.sandbox_image)
                except docker.errors.ImageNotFound:
                    problems.append(
                        f"Sandbox image '{settings.sandbox_image}' not found. "
                        "Run: cd backend/sandbox && docker build -t cad-agent-sandbox:latest ."
                    )
            except Exception:
                problems.append("Docker daemon unavailable. Start Docker or set SANDBOX_RUNTIME=podman.")
        except Exception:
            problems.append("docker SDK unavailable. Run: pip install -r requirements.txt")

    if problems:
        logger.error(
            "启动自检发现 %d 个阻断问题，生成功能将不可用:\n  - %s",
            len(problems), "\n  - ".join(problems),
        )
    else:
        logger.info("启动自检通过: LLM key + Docker + 沙箱镜像就绪")
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    import asyncio
    # Refuse to boot with an unsafe auth config (empty/placeholder token secret,
    # dev code exposure on). This is a hard gate, not a warning.
    settings.assert_auth_config_safe()
    _cleanup_old_files()
    app.state.startup_problems = _startup_self_check()
    # Provision admin + default invite eagerly so misconfig surfaces at boot, not
    # on the first request. Both are no-ops when their config is unset/empty.
    from app.storage.auth import ensure_admin_user, ensure_default_invite_code
    await ensure_admin_user()
    await ensure_default_invite_code()
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    yield
    # Shutdown
    cleanup_task.cancel()
    from app.storage.history import close_db
    await close_db()
    from app.storage.auth import close_db as close_auth_db
    await close_auth_db()
    from app.dfm.rule_store import close_db as close_rules_db
    await close_rules_db()
    from app.dfm.knowledge_graph import close_db as close_kg_db
    await close_kg_db()


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
    app.include_router(onshape_router)

    # WebSocket
    app.websocket("/ws/{session_id}")(websocket_endpoint)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        from fastapi.responses import JSONResponse

        problems = getattr(app.state, "startup_problems", [])
        if problems:
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "problems": problems},
            )
        return {"status": "ready", "problems": []}

    if FRONTEND_DIST.exists():
        assets_dir = FRONTEND_DIST / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def serve_web_app(full_path: str):
            requested = (FRONTEND_DIST / full_path).resolve()
            dist_root = FRONTEND_DIST.resolve()
            if requested.is_file() and str(requested).startswith(str(dist_root)):
                return FileResponse(requested)
            return FileResponse(FRONTEND_DIST / "index.html")

    return app


app = create_app()
