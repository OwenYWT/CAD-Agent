"""
集成测试: 验证完整的生成流程
需要: Docker 运行中, ANTHROPIC_API_KEY 环境变量
"""
import os

import pytest

from app.agent.orchestrator import Orchestrator, ConversationContext
from app.models.schemas import StepUpdate, GenerateResponse

DOCKER_AVAILABLE = os.path.exists("/var/run/docker.sock") or bool(os.environ.get("DOCKER_HOST"))
HAS_API_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))


@pytest.mark.asyncio
@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker not available")
@pytest.mark.skipif(not HAS_API_KEY, reason="ANTHROPIC_API_KEY not set")
async def test_simple_box_generation():
    """测试: 简单盒子通过 REST API 无状态接口生成"""
    orchestrator = Orchestrator()
    response = await orchestrator.generate(
        prompt="一个50x30x20mm的实心长方体",
        output_formats=["step", "stl"],
    )
    assert isinstance(response, GenerateResponse)
    assert response.success, f"生成失败: {response.error}"
    assert response.request_id
    assert any("step" in k for k in response.files)
    assert any("stl" in k for k in response.files)
    assert response.code is not None
    assert "show_object" in response.code
    assert response.attempts <= 3


@pytest.mark.asyncio
@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker not available")
@pytest.mark.skipif(not HAS_API_KEY, reason="ANTHROPIC_API_KEY not set")
async def test_websocket_handle_message():
    """测试: WebSocket 有状态接口生成"""
    orchestrator = Orchestrator()
    context = ConversationContext(session_id="test_ws")
    steps = []
    result = await orchestrator.handle_message(
        context=context,
        user_message="一个50x30x20mm的实心长方体",
        on_step=lambda s: steps.append(s),
    )
    assert result.success
    assert len(steps) >= 3  # planning, retrieving_examples, generating_code, executing


@pytest.mark.asyncio
async def test_code_filter_blocks_dangerous():
    """测试: 代码过滤器阻止危险导入"""
    from app.sandbox.code_filter import validate_code
    assert validate_code("import os")[0] == False
    assert validate_code("import cadquery as cq")[0] == True


@pytest.mark.asyncio
async def test_rest_api_endpoints():
    """测试: REST API 端点可通过 FastAPI TestClient 调用"""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Health check
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

        # Generate endpoint exists (may fail due to no Docker, but 422 = route exists)
        r = await client.post("/api/generate", json={"prompt": "test"})
        assert r.status_code in (200, 500)  # 200 if Docker available, 500 if not
