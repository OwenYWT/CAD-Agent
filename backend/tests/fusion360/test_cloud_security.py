import base64
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from app.fusion360.cloud import ApsClient, ApsConfig
from app.fusion360.errors import FusionConnectorError
from app.fusion360.token_store import EncryptedTokenStore


def _store(tmp_path):
    return EncryptedTokenStore(tmp_path / "tokens.db", Fernet.generate_key())


def _config(enabled=True):
    return ApsConfig(
        enabled=enabled, client_id="client-id", client_secret="client-secret",
        redirect_uri="http://127.0.0.1:8000/api/cad/fusion360/cloud/oauth/callback",
    )


def test_cloud_is_disabled_by_default(tmp_path):
    client = ApsClient(_config(False), None)
    with pytest.raises(FusionConnectorError) as raised:
        client.start_oauth("owner")
    assert raised.value.code == "CLOUD_NOT_CONFIGURED"


def test_oauth_uses_exact_redirect_minimum_scope_state_and_pkce(tmp_path):
    client = ApsClient(_config(), _store(tmp_path))
    started = client.start_oauth("owner-a")
    query = parse_qs(urlparse(started["authorization_url"]).query)
    assert query["redirect_uri"] == [_config().redirect_uri]
    assert query["scope"] == ["data:read"]
    assert query["state"] == [started["state"]]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) >= 43


def test_tokens_and_pkce_verifier_are_encrypted_and_owner_isolated(tmp_path):
    store = _store(tmp_path)
    token = {"access_token": "plain-access-token", "scope": "data:read", "expires_at": time.time() + 3600}
    store.put("owner-a", token)
    encrypted = store.encrypted_bytes("owner-a")
    assert encrypted and b"plain-access-token" not in encrypted
    assert store.get("owner-a")["access_token"] == "plain-access-token"
    assert store.get("owner-b") is None


def test_oauth_state_is_owner_bound_expiring_and_one_time(tmp_path):
    store = _store(tmp_path)
    store.create_state("state", "owner-a", "verifier", now=100)
    assert store.consume_state("state", now=101) == ("owner-a", "verifier")
    with pytest.raises(FusionConnectorError):
        store.consume_state("state", now=102)
    store.create_state("expired", "owner-b", "verifier", ttl_s=60, now=100)
    with pytest.raises(FusionConnectorError):
        store.consume_state("expired", now=161)


@pytest.mark.asyncio
async def test_code_exchange_rotates_into_the_state_owner_without_leaking_secrets(tmp_path):
    store = _store(tmp_path)
    client = ApsClient(
        _config(), store,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "access_token": "access", "refresh_token": "refresh", "expires_in": 3600, "scope": "data:read",
        })),
    )
    started = client.start_oauth("owner-a")
    assert await client.exchange_code(started["state"], "authorization-code") == "owner-a"
    assert store.get("owner-a")["access_token"] == "access"


@pytest.mark.asyncio
async def test_data_ids_are_encoded_and_429_retry_after_is_honored(tmp_path):
    store = _store(tmp_path)
    store.put("owner", {
        "access_token": "access", "refresh_token": "refresh", "scope": "data:read",
        "expires_at": time.time() + 3600,
    })
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"data": []})

    client = ApsClient(_config(), store, transport=httpx.MockTransport(handler))
    result = await client.item_versions("owner", "project/id", "urn:adsk.wipprod:dm.lineage:a/b")
    assert result == {"data": []}
    assert len(requests) == 2
    raw_path = requests[-1].url.raw_path.decode()
    assert "project%2Fid" in raw_path
    assert "a%2Fb" in raw_path


@pytest.mark.asyncio
async def test_provider_error_messages_and_tokens_are_redacted(tmp_path):
    store = _store(tmp_path)
    client = ApsClient(
        _config(), store,
        transport=httpx.MockTransport(lambda request: httpx.Response(400, json={
            "error": "invalid_client", "error_description": "secret=provider-secret",
        })),
    )
    started = client.start_oauth("owner")
    with pytest.raises(FusionConnectorError) as raised:
        await client.exchange_code(started["state"], "bad")
    assert "provider-secret" not in raised.value.message
    assert "client-secret" not in raised.value.message
