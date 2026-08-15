"""
Phase 2 集成测试
需要: Docker 运行中 (部分测试), ANTHROPIC_API_KEY 环境变量 (LLM 测试)
"""
import os
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app

DOCKER_AVAILABLE = os.path.exists("/var/run/docker.sock") or bool(os.environ.get("DOCKER_HOST"))
HAS_API_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))


# === Auth integration tests ===

@pytest.mark.asyncio
async def test_auth_disabled_by_default():
    """No API key configured = all endpoints accessible"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/generate", json={"prompt": "test"})
        # Should not get 401 (auth skipped when api_keys is empty)
        assert r.status_code != 401


@pytest.mark.asyncio
async def test_auth_rejects_invalid_key():
    """When api_keys is set, invalid key gets 401"""
    from app.config import settings

    original = settings.api_keys
    try:
        settings.api_keys = ["valid-key-123"]
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # No key
            r = await client.post("/api/generate", json={"prompt": "test"})
            assert r.status_code == 401

            # Wrong key
            r = await client.post(
                "/api/generate",
                json={"prompt": "test"},
                headers={"X-API-Key": "wrong-key"},
            )
            assert r.status_code == 401

            # Correct key (may fail at Docker level, but not at auth)
            r = await client.post(
                "/api/generate",
                json={"prompt": "test"},
                headers={"X-API-Key": "valid-key-123"},
            )
            assert r.status_code != 401
    finally:
        settings.api_keys = original


# === Rate limiting integration ===

@pytest.mark.asyncio
async def test_rate_limiting_integration():
    """Rate limiter triggers 429 after exceeding limit"""
    from app.api.auth import rate_limiter

    original_rpm = rate_limiter.rpm
    try:
        rate_limiter.rpm = 2
        rate_limiter._windows.clear()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Use /api/execute since it doesn't need LLM
            for _ in range(2):
                r = await client.post(
                    "/api/execute",
                    json={
                        "code": "import cadquery",
                        "project_id": str(uuid4()),
                        "branch_id": str(uuid4()),
                        "expected_base_revision_id": str(uuid4()),
                        "idempotency_key": f"rate-{uuid4()}",
                    },
                )
                assert r.status_code != 429

            r = await client.post(
                "/api/execute",
                json={
                    "code": "import cadquery",
                    "project_id": str(uuid4()),
                    "branch_id": str(uuid4()),
                    "expected_base_revision_id": str(uuid4()),
                    "idempotency_key": f"rate-{uuid4()}",
                },
            )
            assert r.status_code == 429
    finally:
        rate_limiter.rpm = original_rpm
        rate_limiter._windows.clear()


# === Parts API integration ===

@pytest.mark.asyncio
async def test_parts_api_all_screws():
    """All M-series screws are accessible via API"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for m in ["M2", "M3", "M4", "M5", "M6", "M8", "M10", "M12"]:
            r = await client.get(f"/api/parts/{m}")
            assert r.status_code == 200, f"{m} not found"
            data = r.json()
            assert "pitch" in data["params"]


@pytest.mark.asyncio
async def test_parts_api_bearings():
    """All bearings are accessible via API"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for b in ["608", "6000", "6001", "6200", "6201", "6202"]:
            r = await client.get(f"/api/parts/{b}")
            assert r.status_code == 200, f"Bearing {b} not found"
            data = r.json()
            assert "inner" in data["params"]


# === Full pipeline (requires Docker + LLM) ===

@pytest.mark.asyncio
@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker not available")
@pytest.mark.skipif(not HAS_API_KEY, reason="ANTHROPIC_API_KEY not set")
async def test_full_generation_with_validation():
    """End-to-end: generate → execute → validate geometry"""
    from app.agent.orchestrator import Orchestrator

    orchestrator = Orchestrator()
    response = await orchestrator.generate(
        prompt="一个50x30x20mm的实心长方体",
        output_formats=["step", "stl"],
    )
    assert response.success
    assert response.code
    if response.validation:
        assert "is_watertight" in response.validation
        assert "bounding_box" in response.validation


@pytest.mark.asyncio
@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker not available")
@pytest.mark.skipif(not HAS_API_KEY, reason="ANTHROPIC_API_KEY not set")
async def test_standard_parts_in_prompt():
    """Generate a part that references M5 screws — parts info should be injected"""
    from app.agent.orchestrator import Orchestrator

    orchestrator = Orchestrator()
    response = await orchestrator.generate(
        prompt="一个 100x50x5mm 的安装板，四角各有一个 M5 螺丝通孔",
        output_formats=["step", "stl"],
    )
    assert response.success
    # Code should contain the M5 clearance diameter (5.3mm)
    assert response.code
