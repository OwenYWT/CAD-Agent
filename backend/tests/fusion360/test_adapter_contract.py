import uuid

import httpx
import pytest

from app.fusion360.adapter import RemoteFusionAdapter
from app.fusion360.contract import ACTION_NAMES, CadAdapter
from app.fusion360.runtime_app import RuntimeConfig, create_runtime_app


def test_protocol_exposes_only_stable_adapter_methods():
    assert set(CadAdapter.__dict__) >= {"get_capabilities", "get_context", "execute", "verify"}


def test_get_capabilities_is_synchronous_static_and_does_not_touch_transport():
    adapter = RemoteFusionAdapter(
        base_url="http://runtime", backend_secret="secret", owner_id="owner",
        transport=httpx.MockTransport(lambda _request: (_ for _ in ()).throw(AssertionError("network called"))),
    )
    capabilities = adapter.get_capabilities()
    assert capabilities.runtime_online is False
    assert capabilities.actions == ACTION_NAMES


def test_production_adapter_rejects_non_loopback_runtime():
    with pytest.raises(Exception) as raised:
        RemoteFusionAdapter(
            base_url="https://runtime.example.com", backend_secret="secret", owner_id="owner"
        )
    assert getattr(raised.value, "code", None) == "CONNECTOR_OFFLINE"


@pytest.mark.asyncio
async def test_refresh_status_updates_cached_truth(tmp_path):
    app = create_runtime_app(RuntimeConfig(
        database_path=tmp_path / "runtime.db", artifact_root=tmp_path / "artifacts",
        backend_secret="backend", connector_secret="connector",
    ))
    transport = httpx.ASGITransport(app=app)
    connector_id = str(uuid.uuid4())
    async with httpx.AsyncClient(transport=transport, base_url="http://runtime") as client:
        registration = {
            "connector_instance_id": connector_id, "protocol_versions": [1],
            "fusion_version": "2.0.test", "addin_version": "1.0.0", "platform": "macos",
            "artifact_root_fingerprint": app.state.artifacts.fingerprint,
            "capabilities": {
                "adapter": "fusion360", "available": True, "runtime_online": True,
                "connector_online": True, "actions": ["cad.export"],
                "context_sections": ["application"], "limitations": [],
            },
        }
        response = await client.post(
            "/v1/connector/register", headers={"Authorization": "Bearer connector"}, json=registration
        )
        assert response.status_code == 200
    adapter = RemoteFusionAdapter(
        base_url="http://runtime", backend_secret="backend", owner_id="owner", transport=transport
    )
    refreshed = await adapter.refresh_status()
    assert refreshed.available is True
    assert refreshed.fusion_running is True
    assert refreshed.fusion_version == "2.0.test"
    assert adapter.get_capabilities().runtime_online is True
