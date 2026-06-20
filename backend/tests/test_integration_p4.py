"""
Phase 4 integration tests
Web client + batch API + SDK
"""
import ast
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

DOCKER_AVAILABLE = os.path.exists("/var/run/docker.sock") or bool(os.environ.get("DOCKER_HOST"))
HAS_API_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# === Batch API tests ===

@pytest.mark.asyncio
async def test_batch_generate_endpoint_exists():
    """批量生成端点存在"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/batch/generate", json={
            "items": [
                {"prompt": "50x30x20mm长方体"},
                {"prompt": "直径40mm高60mm圆柱"},
            ],
            "max_concurrent": 2,
        })
        # 200 if Docker, 500 if not — either means route exists
        assert r.status_code in (200, 500)


@pytest.mark.asyncio
async def test_async_generate_endpoint_exists():
    """异步生成端点存在"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/generate/async", json={
            "prompt": "test",
        })
        assert r.status_code in (200, 500)
        if r.status_code == 200:
            data = r.json()
            assert "task_id" in data
            assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_task_status_not_found():
    """查询不存在的任务返回 404"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/tasks/nonexistent-id")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_batch_api_module_loads():
    """批量 API 模块可加载"""
    from app.api.batch import router
    assert router is not None


# === Web client file structure tests ===

def test_web_client_files():
    """Web client files are present for browser-only usage."""
    frontend = PROJECT_ROOT / "frontend" / "src"
    assert (frontend / "App.tsx").exists()
    assert (frontend / "hooks" / "useWebSocket.ts").exists()
    assert (frontend / "components" / "ChatPanel.tsx").exists()
    assert (frontend / "components" / "DownloadPanel.tsx").exists()


def test_legacy_client_directory_removed_from_product_surface():
    """The product no longer ships desktop CAD clients."""
    assert not any(path.name == "plugins" for path in PROJECT_ROOT.iterdir())


def test_readme_is_web_first():
    """README documents the browser app, not plugin installation."""
    content = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Web App" in content or "CAD Agent Web" in content
    assert "plugins/" not in content
    assert "SolidWorks" not in content


# === Full batch generate with Docker ===

@pytest.mark.asyncio
@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker not available")
@pytest.mark.skipif(not HAS_API_KEY, reason="ANTHROPIC_API_KEY not set")
async def test_batch_generate_full():
    """批量生成完整测试 (需要 Docker + API Key)"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=180) as client:
        r = await client.post("/api/batch/generate", json={
            "items": [
                {"prompt": "50x30x20mm长方体", "output_formats": ["step"]},
            ],
            "max_concurrent": 1,
        })
        assert r.status_code == 200
        data = r.json()
        assert "results" in data
        assert len(data["results"]) == 1
