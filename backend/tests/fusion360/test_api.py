import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api.auth import rate_limiter, verify_api_key
from app.config import settings
from app.fusion360.api import router


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(settings, "fusion_runtime_backend_secret", "backend")
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.dependency_overrides[verify_api_key] = lambda: "user:test"
    return test_app


@pytest.mark.asyncio
async def test_capabilities_route_is_available_without_runtime_io(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.get("/api/cad/fusion360/capabilities")
    assert response.status_code == 200
    assert response.json()["adapter"] == "fusion360"
    assert response.json()["runtime_online"] is False


@pytest.mark.asyncio
async def test_invalid_action_is_a_structured_validation_error(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.post("/api/cad/fusion360/actions", json={"action": "cad.run_python", "source": "pass"})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "INVALID_ACTION"
    assert "traceback" not in body["details"]


@pytest.mark.asyncio
async def test_invalid_context_uses_cad_error_instead_of_fastapi_detail(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.post("/api/cad/fusion360/context", json={"query": {"sections": ["everything"]}})
    assert response.status_code == 422
    assert "detail" not in response.json()
    assert response.json()["error"]["code"] == "INVALID_ACTION"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        non_object = await client.post("/api/cad/fusion360/context", json=["not", "an", "object"])
    assert non_object.status_code == 422
    assert non_object.json()["error"]["code"] == "INVALID_ACTION"


@pytest.mark.asyncio
async def test_rate_limit_is_returned_as_a_structured_cad_error(app, monkeypatch):
    async def reject(_request, _credential):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    monkeypatch.setattr(rate_limiter, "check", reject)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend") as client:
        response = await client.get("/api/cad/fusion360/capabilities")

    assert response.status_code == 429
    assert "detail" not in response.json()
    assert response.json()["error"] == {
        "code": "RATE_LIMITED",
        "category": "rate_limit",
        "message": "Fusion API rate limit exceeded",
        "retryable": True,
        "details": {},
    }


@pytest.mark.auth
@pytest.mark.asyncio
async def test_authentication_failure_uses_the_structured_cad_error_contract(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "api_keys", [])
    test_app = FastAPI()
    test_app.include_router(router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app), base_url="http://backend"
    ) as client:
        response = await client.get("/api/cad/fusion360/capabilities")

    assert response.status_code == 401
    assert "detail" not in response.json()
    assert response.json()["error"] == {
        "code": "AUTH_REQUIRED",
        "category": "auth",
        "message": "Fusion API authentication is required",
        "retryable": False,
        "details": {},
    }
