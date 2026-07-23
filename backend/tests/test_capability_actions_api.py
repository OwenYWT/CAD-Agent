"""HTTP contracts and owner/path gates for executable CAD Skills actions."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import auth as auth_module
from app.capabilities.runtime import CapabilityRuntime
from app.config import settings
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "cadskills_enable_bambu_lan", False)
    monkeypatch.setattr(settings, "cadskills_isolated_executor", [])
    auth_module.rate_limiter._windows.clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    auth_module.rate_limiter._windows.clear()


def test_upload_returns_owner_scoped_input_and_downloads(client):
    uploaded = client.post(
        "/api/capability-artifacts",
        files={"file": ("part.stl", b"solid test\nendsolid test\n", "model/stl")},
    )
    assert uploaded.status_code == 201
    body = uploaded.json()
    assert body["input"].startswith("uploads/")
    assert body["input"].endswith("/part.stl")
    assert len(body["sha256"]) == 64
    assert "path" not in body

    downloaded = client.get(body["url"])
    assert downloaded.status_code == 200
    assert downloaded.content == b"solid test\nendsolid test\n"


def test_upload_rejects_traversal_and_unknown_file_types(client):
    traversal = client.post(
        "/api/capability-artifacts",
        files={"file": ("../secret.stl", b"x", "model/stl")},
    )
    assert traversal.status_code == 400

    executable = client.post(
        "/api/capability-artifacts",
        files={"file": ("payload.sh", b"exit 0", "text/plain")},
    )
    assert executable.status_code == 400


def test_action_endpoint_uses_allowlisted_runtime_and_hides_host_paths(client, monkeypatch):
    def fake_execute(self, capability, action, params, request_id=None):
        run_id = request_id or "run123"
        output = self.artifacts.write_bytes(run_id, "result.step", b"STEP", suffixes={".step"})
        return {
            "status": "succeeded",
            "capability": capability,
            "action": action,
            "request_id": run_id,
            "data": {"stderr": str(Path(settings.file_storage_dir).resolve())},
            "files": [output],
            "checks": [],
            "command_preview": [str(Path(settings.file_storage_dir).resolve()), "safe"],
            "blocked_reasons": [],
        }

    monkeypatch.setattr(CapabilityRuntime, "execute", fake_execute)
    response = client.post(
        "/api/capability-actions/cad/inspect",
        json={"params": {}, "request_id": "run123"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["files"][0]["url"].endswith("/runs/run123/result.step")
    assert "path" not in body["files"][0]
    assert "$WORKSPACE" in " ".join(body["command_preview"])


def test_unknown_capability_is_404(client):
    response = client.post("/api/capability-actions/shell/run", json={"params": {}})
    assert response.status_code == 404


def test_bambu_network_is_deployment_blocked_by_default(client):
    response = client.post(
        "/api/capability-actions/bambu-labs/status",
        json={
            "params": {
                "execute": True,
                "confirm_status": True,
                "host": "192.0.2.1",
                "access_code": "do-not-log-me",
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "blocked"
    assert body["blocked_reasons"]
    assert "do-not-log-me" not in str(body)
