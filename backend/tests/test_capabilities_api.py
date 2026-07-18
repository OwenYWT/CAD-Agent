"""Contract tests for the read-only cadskills capability registry."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import auth as auth_module
from app.capabilities import registry
from app.config import settings
from app.main import create_app


EXPECTED_IDS = [
    "cad",
    "cad-viewer",
    "step-parts",
    "dxf",
    "urdf",
    "srdf",
    "sdf",
    "sendcutsend",
    "gcode",
    "bambu-labs",
    "implicit-cad",
]


@pytest.fixture
def client():
    auth_module.rate_limiter._windows.clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    auth_module.rate_limiter._windows.clear()


def test_list_returns_all_11_pinned_manifests(client):
    response = client.get("/api/capabilities")

    assert response.status_code == 200
    manifests = response.json()
    assert [item["id"] for item in manifests] == EXPECTED_IDS
    assert all(item["upstream"] == {
        "version": "0.3.9",
        "commit": "fdbb4b4fb62d95ae298cfe9a46fdc7092bdaf423",
        "url": "https://github.com/earthtojake/text-to-cad",
    } for item in manifests)

    required_fields = {
        "id", "name", "group", "summary", "maturity", "risk_level",
        "actions", "accepts", "produces", "dependencies", "available",
        "blocked_reasons", "upstream",
    }
    for manifest in manifests:
        assert required_fields == set(manifest)
        assert manifest["actions"]
        assert manifest["accepts"]
        assert manifest["produces"]
        assert manifest["dependencies"]
        expected_reasons = [
            dependency["detail"]
            for dependency in manifest["dependencies"]
            if dependency["required"] and not dependency["available"]
        ]
        assert manifest["blocked_reasons"] == expected_reasons
        assert manifest["available"] is (not expected_reasons)


def test_get_returns_one_manifest_and_unknown_is_404(client):
    response = client.get("/api/capabilities/cad")
    assert response.status_code == 200
    assert response.json()["id"] == "cad"

    missing = client.get("/api/capabilities/not-a-capability")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Capability 'not-a-capability' not found"


@pytest.mark.auth
def test_capability_routes_use_api_auth(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "api_keys", ["capability-secret"])
    monkeypatch.setattr(settings, "auth_token_secret", "test-only-capability-auth-secret-with-sufficient-length")
    with TestClient(create_app()) as client:
        assert client.get("/api/capabilities").status_code == 401
        assert client.get(
            "/api/capabilities/cad",
            headers={"Authorization": "Bearer capability-secret"},
        ).status_code == 200


def test_capability_routes_use_shared_rate_limiter(client):
    original_rpm = auth_module.rate_limiter.rpm
    auth_module.rate_limiter.rpm = 1
    auth_module.rate_limiter._windows.clear()
    try:
        assert client.get("/api/capabilities").status_code == 200
        assert client.get("/api/capabilities/cad").status_code == 429
    finally:
        auth_module.rate_limiter.rpm = original_rpm
        auth_module.rate_limiter._windows.clear()


def test_vendored_runtime_is_located_from_repo_not_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    manifests = registry.list_capabilities()

    assert registry.REPO_ROOT == Path(__file__).resolve().parents[2]
    assert len(manifests) == 11
    for manifest in manifests:
        vendored = next(dep for dep in manifest.dependencies if dep.id == "vendored-runtime")
        assert vendored.available is True


def test_missing_required_dependency_never_reports_available(monkeypatch):
    monkeypatch.setattr(registry, "_command_path", lambda *names: None)
    monkeypatch.setattr(registry, "_module_available", lambda name: False)
    manifests = {item.id: item for item in registry.list_capabilities()}

    assert manifests["cad"].available is False
    assert manifests["cad"].blocked_reasons
    assert manifests["gcode"].available is False
    assert manifests["implicit-cad"].available is False


def test_existing_dxf_module_is_a_real_local_backend(monkeypatch):
    monkeypatch.setattr(registry, "_module_available", lambda name: name == "ezdxf")
    monkeypatch.setattr(registry, "_command_path", lambda *names: None)

    dxf = registry.get_capability("dxf")
    assert dxf is not None
    runtime = next(dep for dep in dxf.dependencies if dep.id == "dxf-runtime")
    assert runtime.available is True
    assert "ezdxf" in runtime.detail
    assert dxf.available is True


def test_existing_sandbox_is_recognized_as_cad_backend(monkeypatch):
    monkeypatch.setattr(registry, "_module_available", lambda name: False)
    monkeypatch.setattr(registry, "_sandbox_assets_available", lambda: True)
    monkeypatch.setattr(registry, "_command_path", lambda *names: "/usr/bin/docker")

    cad = registry.get_capability("cad")
    assert cad is not None
    runtime = next(dep for dep in cad.dependencies if dep.id == "cad-runtime")
    assert runtime.available is True
    assert "backend/sandbox" in runtime.detail
    assert cad.available is True


def test_optional_viewer_reuse_launcher_does_not_block_server(monkeypatch):
    monkeypatch.setattr(registry, "_command_path", lambda *names: "/usr/bin/node" if "node" in names else None)

    viewer = registry.get_capability("cad-viewer")
    assert viewer is not None
    launcher = next(dep for dep in viewer.dependencies if dep.id == "viewer-agent-launcher")
    assert launcher.required is False
    assert launcher.available is False
    assert launcher.detail not in viewer.blocked_reasons
    assert viewer.available is True


def test_python_dependency_uses_project_311_baseline():
    dependency = registry._python_runtime_dependency()
    assert "3.11 or newer" in dependency.detail


def test_uploaded_generators_are_not_reported_available_without_isolator(monkeypatch):
    monkeypatch.setattr(settings, "cadskills_isolated_executor", [])
    manifests = {item.id: item for item in registry.list_capabilities()}

    for capability_id in ("urdf", "srdf", "sdf", "implicit-cad"):
        manifest = manifests[capability_id]
        assert manifest.available is False
        assert any("CADSKILLS_ISOLATED_EXECUTOR" in reason for reason in manifest.blocked_reasons)
