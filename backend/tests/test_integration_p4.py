"""
Phase 4 integration tests
Web client + batch API + SDK
"""
import ast
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import batch as batch_api
from app.main import app
from app.models.schemas import GenerateResponse
from app.storage import history

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
        # Durable identity is required before work can be accepted.
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_async_generate_endpoint_persists_and_returns_terminal_result(
    monkeypatch,
):
    """异步 API 返回持久 WorkflowRun，并从同一投影读取终态。"""
    workflow_run_id = uuid4()
    identity = {
        "project_id": str(uuid4()),
        "branch_id": str(uuid4()),
        "expected_base_revision_id": str(uuid4()),
        "idempotency_key": f"async-{uuid4()}",
    }

    async def submit(_principal, **_kwargs):
        return SimpleNamespace(workflow_run_id=workflow_run_id)

    async def projection(_principal, queried_workflow_run_id):
        assert queried_workflow_run_id == workflow_run_id
        return GenerateResponse(
            request_id=str(workflow_run_id),
            workflow_run_id=workflow_run_id,
            success=True,
            task_status="succeeded",
            files={"step": f"/api/files/{workflow_run_id}/result.step"},
        )

    monkeypatch.setattr(batch_api, "submit_durable_workflow", submit)
    monkeypatch.setattr(batch_api, "get_compatibility_response", projection)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/generate/async", json={
            "prompt": "test",
            **identity,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "pending"
        task_id = data["task_id"]

        status_response = await client.get(f"/api/tasks/{task_id}")
        assert status_response.status_code == 200
        terminal = status_response.json()

    assert terminal["status"] == "completed"
    assert terminal["result"]["request_id"] == str(workflow_run_id)


@pytest.mark.asyncio
async def test_task_status_not_found():
    """查询不存在的任务返回 404"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/tasks/nonexistent-id")
        assert r.status_code == 404
    await history.close_db()


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
    assert (frontend / "components" / "workspace" / "EngineeringWorkspace.tsx").exists()
    assert (frontend / "components" / "export" / "ExportDialog.tsx").exists()


def test_legacy_client_directory_removed_from_product_surface():
    """The product no longer ships desktop CAD clients."""
    assert not any(path.name == "plugins" for path in PROJECT_ROOT.iterdir())


def test_readme_is_web_first():
    """README documents the browser app, not plugin installation."""
    content = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "# WordsWave CAD Agent" in content
    assert "浏览器工作区" in content
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
