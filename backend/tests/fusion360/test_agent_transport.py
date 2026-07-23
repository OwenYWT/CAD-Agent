import hashlib
import io
import json
import os
import queue
import stat
import sys
import urllib.error
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from fusion_addin.CADAgentFusionConnector.agent_transport import (
    AgentHttpClient,
    AgentTransportError,
    CloudAgentWorker,
    load_bearer_token,
)
from fusion_addin.CADAgentFusionConnector.config import ConnectorConfig, load_config


def _write_config(tmp_path, **overrides):
    token = tmp_path / "agent.token"
    token.write_text("test-token-which-is-long-enough", encoding="utf-8")
    if os.name != "nt":
        token.chmod(0o600)
    value = {
        "mode": "cloud_agent",
        "agent_url": "https://agent.example.test",
        "agent_token_file": str(token),
        "connector_instance_id": str(uuid.uuid4()),
        "artifact_root": str(tmp_path / "artifacts"),
        "journal_path": str(tmp_path / "journal.json"),
    }
    value.update(overrides)
    path = tmp_path / "connector.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "url",
    [
        "http://agent.example.test",
        "http://localhost:8000",
        "https://user:password@agent.example.test",
        "https://agent.example.test?redirect=x",
        "https://agent.example.test/#fragment",
    ],
)
def test_cloud_config_requires_unambiguous_https(url, tmp_path):
    with pytest.raises(RuntimeError):
        load_config(_write_config(tmp_path, agent_url=url))


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://[::1]:8000"])
def test_cloud_config_allows_explicit_loopback_http_for_development(url, tmp_path):
    assert load_config(_write_config(tmp_path, agent_url=url)).agent_url == url


def test_cloud_mode_is_default_and_local_runtime_is_explicit(tmp_path):
    path = _write_config(tmp_path)
    value = json.loads(path.read_text())
    value.pop("mode")
    path.write_text(json.dumps(value))
    assert load_config(path).mode == "cloud_agent"

    runtime = _write_config(
        tmp_path,
        mode="local_runtime",
        runtime_url="http://127.0.0.1:8765",
        connector_secret="x" * 32,
    )
    assert load_config(runtime).mode == "local_runtime"


def test_token_file_permissions_and_value_are_validated(tmp_path):
    token = tmp_path / "token"
    token.write_text("x" * 32)
    if os.name != "nt":
        token.chmod(0o644)
        with pytest.raises(AgentTransportError, match="permissions"):
            load_bearer_token(token)
        token.chmod(0o600)
    assert load_bearer_token(token) == "x" * 32
    token.write_text("short")
    with pytest.raises(AgentTransportError):
        load_bearer_token(token)


class _Response:
    def __init__(self, body=b"{}", status=200, content_type="application/json"):
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


class _Opener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _config(tmp_path, **overrides):
    token = tmp_path / "token"
    token.write_text("t" * 32)
    if os.name != "nt":
        token.chmod(0o600)
    values = dict(
        connector_instance_id=str(uuid.uuid4()),
        artifact_root=tmp_path / "artifacts",
        journal_path=tmp_path / "journal.json",
        mode="cloud_agent",
        agent_url="https://agent.test",
        agent_token_file=token,
        max_response_bytes=512,
        max_request_bytes=512,
        retry_attempts=2,
    )
    values.update(overrides)
    return ConnectorConfig(**values)


def test_http_client_rejects_redirect_and_oversize_or_non_json_response(tmp_path):
    redirect = urllib.error.HTTPError(
        "https://agent.test/plan", 302, "Found", {"Location": "https://evil.test"}, io.BytesIO(b"")
    )
    client = AgentHttpClient(_config(tmp_path, max_response_bytes=64), opener=_Opener([redirect]), sleeper=lambda _: None)
    with pytest.raises(AgentTransportError) as error:
        client.plan({"request_id": str(uuid.uuid4())})
    assert error.value.code == "REDIRECT_REJECTED"

    client = AgentHttpClient(_config(tmp_path, max_response_bytes=64), opener=_Opener([_Response(b"x" * 65)]))
    with pytest.raises(AgentTransportError) as error:
        client.plan({"request_id": str(uuid.uuid4())})
    assert error.value.code == "RESPONSE_TOO_LARGE"

    client = AgentHttpClient(_config(tmp_path), opener=_Opener([_Response(b"not-json")]))
    with pytest.raises(AgentTransportError) as error:
        client.plan({"request_id": str(uuid.uuid4())})
    assert error.value.code == "INVALID_RESPONSE"


def test_plan_and_report_retry_idempotently_but_upload_does_not(tmp_path):
    request_id = str(uuid.uuid4())
    opener = _Opener([urllib.error.URLError("offline"), _Response(b'{"status":"no_action"}')])
    client = AgentHttpClient(_config(tmp_path), opener=opener, sleeper=lambda _: None)
    assert client.plan({"request_id": request_id})["status"] == "no_action"
    assert len(opener.requests) == 2
    assert all(req.get_header("Idempotency-key") == request_id for req, _ in opener.requests)

    opener = _Opener([urllib.error.URLError("offline"), _Response(b'{"accepted":true}')])
    client = AgentHttpClient(_config(tmp_path), opener=opener, sleeper=lambda _: None)
    assert client.report({"request_id": request_id}) == {"accepted": True}
    assert len(opener.requests) == 2


def test_retryable_rate_limit_honors_bounded_retry_after(tmp_path):
    request_id = str(uuid.uuid4())
    limited = urllib.error.HTTPError(
        "https://agent.test/plan", 429, "limited", {"Retry-After": "3"}, io.BytesIO(b"")
    )
    opener = _Opener([limited, _Response(b'{"status":"no_action"}')])
    delays = []
    client = AgentHttpClient(_config(tmp_path), opener=opener, sleeper=delays.append)

    assert client.plan({"request_id": request_id})["status"] == "no_action"
    assert delays == [3.0]
    assert AgentHttpClient._retry_after("999") == 30.0


def test_streaming_artifact_upload_is_bound_to_exact_consent(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    artifact = root / "part.step"
    content = b"ISO-10303-21;" * 1000
    artifact.write_bytes(content)
    consent = {
        "request_id": str(uuid.uuid4()),
        "artifact_id": str(uuid.uuid4()),
        "filename": artifact.name,
        "format": "step",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "upload_authorized": True,
        "f3d_upload_authorized": False,
    }
    opener = _Opener([_Response(b'{"accepted":true}')])
    client = AgentHttpClient(_config(tmp_path), opener=opener)
    assert client.upload_artifact(artifact, consent) == {"accepted": True}
    request = opener.requests[0][0]
    assert not isinstance(request.data, (bytes, bytearray))
    assert b"".join(request.data) == content
    assert request.get_header("Content-length") == str(len(content))

    with pytest.raises(AgentTransportError):
        client.upload_artifact(artifact, {**consent, "sha256": "0" * 64})
    f3d = root / "design.f3d"
    f3d.write_bytes(content)
    with pytest.raises(AgentTransportError, match="F3D"):
        client.upload_artifact(
            f3d,
            {**consent, "filename": f3d.name, "format": "f3d", "f3d_upload_authorized": False},
        )


def test_transport_errors_and_repr_never_disclose_token(tmp_path):
    token = "super-secret-token-value-which-must-not-leak"
    config = _config(tmp_path)
    config.agent_token_file.write_text(token)
    if os.name != "nt":
        config.agent_token_file.chmod(0o600)
    client = AgentHttpClient(
        config,
        opener=_Opener([urllib.error.URLError(token), urllib.error.URLError(token)]),
        sleeper=lambda _: None,
    )
    with pytest.raises(AgentTransportError) as error:
        client.plan({"request_id": str(uuid.uuid4())})
    assert token not in str(error.value)
    assert token not in repr(client)


class _FakeClient:
    credential_subject = "bearer-sha256:" + "a" * 64

    def capabilities(self):
        return {
            "contract_versions": ["1.0.0"],
            "actions": [
                "cad.update_parameter", "cad.update_feature_parameter", "cad.create_sketch",
                "cad.create_extrude", "cad.create_hole", "cad.create_fillet",
                "cad.create_chamfer", "cad.update_entity_properties", "cad.save_document",
                "cad.save_as", "cad.export",
            ],
        }

    def plan(self, body):
        return {"status": "no_action", "request_id": body["request_id"]}

    def report(self, body):
        return {"accepted": True, "request_id": body["request_id"]}

    def upload_artifact(self, path, consent):
        return {"accepted": True}

    def heartbeat(self, body):
        return {"status": "online", "connector_instance_id": body["connector_instance_id"]}


def test_worker_has_bounded_pure_json_queues_signals_and_stops_cleanly(tmp_path):
    inbound = queue.Queue(maxsize=1)
    outbound = queue.Queue(maxsize=1)
    signals = []
    worker = CloudAgentWorker(
        _config(tmp_path), inbound, outbound, lambda: signals.append("signal"), client=_FakeClient()
    )
    assert not hasattr(worker, "facade")
    worker.start()
    request_id = str(uuid.uuid4())
    assert worker.submit({"kind": "plan", "payload": {"request_id": request_id}})
    message = outbound.get(timeout=2)
    assert message == {
        "kind": "plan_result",
        "request_id": request_id,
        "payload": {"status": "no_action", "request_id": request_id},
        "credential_subject": _FakeClient.credential_subject,
    }
    assert signals == ["signal"]
    assert worker.stop(timeout=2)
    assert not worker.submit({"kind": "plan", "payload": {"request_id": request_id}})


def test_worker_rejects_non_json_and_full_queue_without_blocking(tmp_path):
    inbound = queue.Queue(maxsize=1)
    worker = CloudAgentWorker(_config(tmp_path), inbound, queue.Queue(maxsize=1), lambda: None, client=_FakeClient())
    with pytest.raises(AgentTransportError):
        worker.submit({"kind": "plan", "payload": {"bad": object()}})
    assert worker.submit({"kind": "plan", "payload": {"request_id": str(uuid.uuid4())}})
    assert not worker.submit({"kind": "plan", "payload": {"request_id": str(uuid.uuid4())}})


def test_worker_sends_bounded_heartbeat_without_autodesk_objects(tmp_path):
    class Client(_FakeClient):
        def __init__(self):
            self.heartbeats = []
            self.capability_calls = 0

        def capabilities(self):
            self.capability_calls += 1
            return super().capabilities()

        def heartbeat(self, body):
            self.heartbeats.append(body)
            return super().heartbeat(body)

    client = Client()
    connector_id = str(uuid.uuid4())
    registration = {
        "connector_instance_id": connector_id,
        "fusion_version": "2.x",
        "addin_version": "1.0.0",
        "platform": "macos",
        "capabilities": {
            "adapter": "fusion360", "available": True, "connector_online": True,
            "fusion_running": True, "actions": [], "context_sections": [],
        },
    }
    worker = CloudAgentWorker(
        _config(tmp_path), queue.Queue(maxsize=1), queue.Queue(maxsize=1),
        lambda: None, client=client, registration=registration,
    )
    worker.start()
    for _ in range(100):
        if client.heartbeats:
            break
        import time
        time.sleep(0.01)
    assert worker.stop(timeout=2)
    assert len(client.heartbeats) == 1
    assert client.capability_calls == 1
    assert client.heartbeats[0]["connector_instance_id"] == connector_id
    assert client.heartbeats[0]["sent_at"].endswith("Z")


def test_worker_fails_plan_closed_when_agent_contract_is_incompatible(tmp_path):
    class Incompatible(_FakeClient):
        def capabilities(self):
            return {"contract_versions": ["2.0.0"], "actions": []}

    connector_id = str(uuid.uuid4())
    inbound = queue.Queue(maxsize=1)
    outbound = queue.Queue(maxsize=1)
    worker = CloudAgentWorker(
        _config(tmp_path), inbound, outbound, lambda: None,
        client=Incompatible(),
        registration={
            "connector_instance_id": connector_id,
            "fusion_version": "2.x", "addin_version": "1.0.0", "platform": "macos",
            "capabilities": {
                "adapter": "fusion360", "available": True, "connector_online": True,
                "fusion_running": True, "actions": ["cad.update_parameter"],
                "context_sections": ["document"],
            },
        },
    )
    request_id = str(uuid.uuid4())
    assert worker.submit({"kind": "plan", "payload": {"request_id": request_id}})
    worker.start()
    message = outbound.get(timeout=2)
    assert worker.stop(timeout=2)
    assert message["kind"] == "transport_error"
    assert message["error"]["code"] == "PROTOCOL_MISMATCH"
