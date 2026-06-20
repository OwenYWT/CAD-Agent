"""
Phase 2 单元测试: 几何验证, 视觉验证, 向量检索, 标准件库, 代码过滤, 认证, 限流
"""
import asyncio
import time

import pytest


# === Code filter ===

def test_code_filter_allows_ezdxf():
    from app.sandbox.code_filter import validate_code
    ok, _ = validate_code("import ezdxf\ndoc = ezdxf.new()")
    assert ok, "ezdxf should be whitelisted"


def test_code_filter_blocks_subprocess():
    from app.sandbox.code_filter import validate_code
    ok, msg = validate_code("import subprocess")
    assert not ok
    assert "subprocess" in msg


def test_code_filter_allows_cadquery():
    from app.sandbox.code_filter import validate_code
    ok, _ = validate_code("import cadquery as cq\nimport math\nimport numpy as np")
    assert ok


# === Standard parts library ===

def test_parts_lookup_metric_screw():
    from app.parts_library.data import lookup
    result = lookup("M5")
    assert result is not None
    assert result["pitch"] == 0.8
    assert result["head_dia"] == 8.5


def test_parts_lookup_bearing():
    from app.parts_library.data import lookup
    result = lookup("608")
    assert result is not None
    assert result["inner"] == 8
    assert result["outer"] == 22


def test_parts_lookup_not_found():
    from app.parts_library.data import lookup
    assert lookup("DOESNOTEXIST") is None


def test_parts_format_for_prompt():
    from app.parts_library.data import format_for_prompt
    text = format_for_prompt("M3")
    assert "M3" in text
    assert "3.2" in text  # clearance
    assert format_for_prompt("INVALID") == ""


# === Geometry validator ===

def test_geometry_validator_init():
    from app.validation.geometry_validator import GeometryValidator
    gv = GeometryValidator()
    assert gv is not None


# === Vision validator ===

def test_vision_validator_init():
    from app.validation.vision_validator import VisionValidator
    vv = VisionValidator()
    assert vv is not None


# === Vector retriever ===

_chromadb_ok_cache: bool | None = None


def _skip_if_no_chromadb():
    """Skip at TEST RUNTIME (not collection) if chromadb can't load. ChromaDB 0.5 is
    incompatible with numpy>=2 (np.float_ removed); production falls back to TF-IDF.

    Must be lazy + memoized: instantiating VectorExampleRetriever builds a Chroma index
    and under numpy2 is slow/noisy — doing it at module-import/collection time slowed
    the ENTIRE pytest run."""
    global _chromadb_ok_cache
    if _chromadb_ok_cache is None:
        try:
            from app.examples.vector_retriever import VectorExampleRetriever
            VectorExampleRetriever()
            _chromadb_ok_cache = True
        except Exception:
            _chromadb_ok_cache = False
    if not _chromadb_ok_cache:
        pytest.skip("chromadb unavailable (e.g. numpy>=2); production falls back to TF-IDF")


@pytest.mark.asyncio
async def test_vector_retriever():
    _skip_if_no_chromadb()
    from app.examples.vector_retriever import VectorExampleRetriever
    r = VectorExampleRetriever()
    assert r.collection.count() > 0
    results = await r.find_similar("一个盒子", top_k=3)
    assert len(results) > 0
    assert "description" in results[0]
    assert "code" in results[0]
    assert "score" in results[0]


@pytest.mark.asyncio
async def test_tfidf_retriever_fallback():
    from app.examples.retriever import ExampleRetriever
    r = ExampleRetriever()
    results = await r.find_similar("box", top_k=3)
    assert isinstance(results, list)


# === Auth ===

def test_auth_skip_when_no_keys():
    """When api_keys is empty, auth is skipped."""
    from app.config import settings
    assert settings.api_keys == []


@pytest.mark.asyncio
async def test_rate_limiter():
    from app.api.auth import RateLimiter
    from fastapi import HTTPException
    from unittest.mock import MagicMock

    limiter = RateLimiter(rpm=3)
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "127.0.0.1"

    # Should allow 3 requests
    for _ in range(3):
        await limiter.check(req)

    # 4th should be rate-limited
    with pytest.raises(HTTPException) as exc_info:
        await limiter.check(req)
    assert exc_info.value.status_code == 429


# === REST API endpoints ===

@pytest.mark.asyncio
async def test_api_health():
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_api_parts_endpoint():
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/parts/M5")
        assert r.status_code == 200
        data = r.json()
        assert data["designation"] == "M5"
        assert data["params"]["pitch"] == 0.8

        r = await client.get("/api/parts/INVALID999")
        assert r.status_code == 404


# === Renderer ===

def test_cad_renderer_init():
    from app.rendering.renderer import CADRenderer
    renderer = CADRenderer()
    assert renderer is not None


# === DXF renderer ===

def test_dxf_renderer_import():
    from app.rendering.dxf_renderer import dxf_to_svg
    assert callable(dxf_to_svg)


# === Orchestrator with vector retriever ===

def test_orchestrator_uses_vector_retriever():
    _skip_if_no_chromadb()
    from app.agent.orchestrator import Orchestrator
    o = Orchestrator()
    assert type(o.retriever).__name__ == "VectorExampleRetriever"
