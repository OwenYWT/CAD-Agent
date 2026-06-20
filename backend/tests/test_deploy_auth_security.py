"""Deploy-readiness tests — AUTH + SECURITY angle.

Hermetic: no Docker, no LLM, no network. The sandbox executor / orchestrator are
faked where a route needs one; auth/files/code_filter/WS paths run for real.

Covers:
  1. Auth OFF by default (api_keys=[]) -> endpoints reachable.
  2. Auth ON (api_keys=['k1']) -> 401 without creds, OK with Bearer, OK with X-API-Key.
  3. /api/files path-traversal hardening: '../', encoded backslash, abs/, disallowed
     extensions (.txt/.exe), bad request_id -> 400; valid-but-missing -> 404.
  4. code_filter blocks os/subprocess/sys/__import__/eval/exec/open/getattr-globals/
     dunder attrs; ALLOWS cadquery/math/numpy/ezdxf. Tested directly AND via /api/execute.
  5. WS auth: bad session_id -> close 4001; api_keys set + missing/bad token -> 4003.
  6. Rate limiter: low rpm -> 429 after the budget is spent.
"""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.main as appmain
from app.config import settings
from app.api import auth as auth_mod
from app.api import websocket as ws_mod
from app.models.schemas import GenerateResponse
from app.sandbox.code_filter import validate_code


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def client():
    return TestClient(appmain.app)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Per-test isolation: storage dir, auth OFF by default, rate limiter reset."""
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "api_keys", [])
    # Reset the shared rate-limiter singleton state before each test so tests
    # don't leak request counts into each other.
    orig_rpm = auth_mod.rate_limiter.rpm
    auth_mod.rate_limiter._windows.clear()
    yield
    auth_mod.rate_limiter.rpm = orig_rpm
    auth_mod.rate_limiter._windows.clear()


class _FakeOrch:
    """Minimal orchestrator stub for /api/execute: validate_code still runs inside
    the route's orchestrator.execute_code in the REAL orchestrator, but for the
    auth/rate-limit tests we only need a successful echo."""

    async def execute_code(self, code, output_formats=None):
        # Mirror the real orchestrator's code_filter gate so the execute-path
        # code_filter test is genuine.
        ok, msg = validate_code(code)
        if not ok:
            return GenerateResponse(
                request_id="r", success=False,
                error={"type": "ValidationError", "message": msg},
            )
        return GenerateResponse(request_id="r", success=True, code=code)


@pytest.fixture
def fake_orch():
    """Inject a fake orchestrator into the route singleton; reset on teardown."""
    prev = ws_mod._orchestrator
    ws_mod._orchestrator = _FakeOrch()
    yield ws_mod._orchestrator
    ws_mod._orchestrator = prev


# --------------------------------------------------------------------------- #
# 1. Auth OFF by default
# --------------------------------------------------------------------------- #

def test_auth_off_health_reachable(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_auth_off_parts_reachable_no_creds(client):
    """api_keys=[] -> verify_api_key is a no-op; route runs and returns its own
    404 for an unknown part (NOT 401)."""
    r = client.get("/api/parts/DOES-NOT-EXIST-9999")
    assert r.status_code == 404


def test_auth_off_execute_reachable_no_creds(client, fake_orch):
    r = client.post("/api/execute", json={"code": "result = 1", "output_formats": ["stl"]})
    assert r.status_code == 200
    assert r.json()["success"] is True


# --------------------------------------------------------------------------- #
# 2. Auth ON
# --------------------------------------------------------------------------- #

def test_auth_on_rejects_without_credentials(client, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", ["k1"])
    r = client.get("/api/parts/DOES-NOT-EXIST-9999")
    assert r.status_code == 401


def test_auth_on_accepts_bearer(client, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", ["k1"])
    r = client.get(
        "/api/parts/DOES-NOT-EXIST-9999",
        headers={"Authorization": "Bearer k1"},
    )
    # Passes auth -> reaches route -> route's own 404 (not 401)
    assert r.status_code == 404


def test_auth_on_accepts_x_api_key_header(client, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", ["k1"])
    r = client.get(
        "/api/parts/DOES-NOT-EXIST-9999",
        headers={"X-API-Key": "k1"},
    )
    assert r.status_code == 404


def test_auth_on_rejects_wrong_bearer(client, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", ["k1"])
    r = client.get(
        "/api/parts/DOES-NOT-EXIST-9999",
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert r.status_code == 401


def test_auth_on_rejects_wrong_x_api_key(client, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", ["k1"])
    r = client.get(
        "/api/parts/DOES-NOT-EXIST-9999",
        headers={"X-API-Key": "wrong-key"},
    )
    assert r.status_code == 401


def test_auth_on_protects_execute(client, monkeypatch, fake_orch):
    monkeypatch.setattr(settings, "api_keys", ["k1"])
    # without creds -> 401
    r = client.post("/api/execute", json={"code": "result = 1"})
    assert r.status_code == 401
    # with bearer -> success
    r = client.post(
        "/api/execute",
        json={"code": "result = 1"},
        headers={"Authorization": "Bearer k1"},
    )
    assert r.status_code == 200
    assert r.json()["success"] is True


# --------------------------------------------------------------------------- #
# 3. /api/files path-traversal hardening
# --------------------------------------------------------------------------- #

def test_files_disallowed_extension_txt(client):
    r = client.get("/api/files/abc123/secret.txt")
    assert r.status_code == 400


def test_files_disallowed_extension_exe(client):
    r = client.get("/api/files/abc123/payload.exe")
    assert r.status_code == 400


def test_files_encoded_backslash_filename(client):
    # %5C decodes to a backslash inside the filename segment -> rejected (400)
    r = client.get("/api/files/abc123/a%5Cb.stl")
    assert r.status_code == 400


def test_files_bad_request_id_with_dot(client):
    # request_id with a '.' fails the safe-id regex (also blocks '..' traversal)
    r = client.get("/api/files/a.b/result.stl")
    assert r.status_code == 400


def test_files_valid_but_missing_is_404(client):
    # Valid id + allowed extension, file does not exist -> 404 (NOT 400/500)
    r = client.get("/api/files/abc123/result.stl")
    assert r.status_code == 404


def test_files_handler_rejects_dotdot_request_id():
    """Call the handler directly with a '..' request_id (URL normalization would
    otherwise strip it before it reaches the route) -> 400, never escapes storage."""
    import asyncio
    from fastapi import HTTPException
    from app.api.files import download_file

    with pytest.raises(HTTPException) as ei:
        asyncio.run(download_file("..", "result.stl"))
    assert ei.value.status_code == 400


def test_files_handler_rejects_traversal_filename():
    import asyncio
    from fastapi import HTTPException
    from app.api.files import download_file

    with pytest.raises(HTTPException) as ei:
        asyncio.run(download_file("abc123", "../../../etc/passwd"))
    assert ei.value.status_code == 400


def test_files_handler_rejects_absolute_path_filename():
    import asyncio
    from fastapi import HTTPException
    from app.api.files import download_file

    with pytest.raises(HTTPException) as ei:
        asyncio.run(download_file("abc123", "/etc/passwd"))
    assert ei.value.status_code == 400


def test_files_handler_serves_existing_allowed_file(tmp_path, monkeypatch):
    """Sanity: the hardening doesn't break the happy path — a real file in storage
    is served with 200."""
    import asyncio
    from app.api.files import download_file

    storage = tmp_path / "files"
    (storage / "abc123").mkdir(parents=True)
    f = storage / "abc123" / "result.stl"
    f.write_text("solid x\nendsolid x\n")
    monkeypatch.setattr(settings, "file_storage_dir", str(storage))

    resp = asyncio.run(download_file("abc123", "result.stl"))
    # FileResponse pointing at the real file inside storage
    assert str(f.resolve()) == resp.path


# --------------------------------------------------------------------------- #
# 4. code_filter — directly AND via /api/execute
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("code", [
    "import os",
    "import subprocess",
    "import sys",
    "import importlib",
    '__import__("os")',
    'eval("1+1")',
    'exec("x = 1")',
    'open("/etc/passwd")',
    'getattr(x, "__globals__")',
    "x.__globals__",
    "x.__builtins__",
    "x.__subclasses__",
    'from os import path',
])
def test_code_filter_blocks_dangerous(code):
    ok, msg = validate_code(code)
    assert ok is False
    assert msg  # a non-empty rejection reason


@pytest.mark.parametrize("code", [
    "import cadquery as cq\nresult = cq.Workplane('XY').box(1, 1, 1)",
    "import math\nr = math.pi * 2",
    "import numpy as np\na = np.zeros(3)",
    "import ezdxf\ndoc = ezdxf.new()",
    "from cadquery import Workplane\nresult = Workplane()",
    "w = 20\nh = 30\nresult = box(w, h)\nshow_object(result)",
])
def test_code_filter_allows_safe(code):
    ok, msg = validate_code(code)
    assert ok is True, f"expected allowed, got rejection: {msg}"
    assert msg is None


def test_execute_route_rejects_blocked_import(client, fake_orch):
    """The /api/execute path surfaces a code_filter rejection as a 500 with a
    ValidationError body (route returns 500 when response.success is False)."""
    r = client.post("/api/execute", json={"code": "import os\nresult = 1"})
    assert r.status_code == 500
    body = r.json()
    assert body["success"] is False
    assert body["error"]["type"] == "ValidationError"


def test_execute_route_allows_safe_code(client, fake_orch):
    r = client.post(
        "/api/execute",
        json={"code": "import cadquery as cq\nresult = cq.Workplane().box(1,1,1)"},
    )
    assert r.status_code == 200
    assert r.json()["success"] is True


# --------------------------------------------------------------------------- #
# 5. WebSocket auth
# --------------------------------------------------------------------------- #

def test_ws_bad_session_id_closes_4001(client, fake_orch):
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/ws/bad session!") as wsx:
            wsx.receive_text()
    assert ei.value.code == 4001


def test_ws_missing_token_closes_4003(client, monkeypatch, fake_orch):
    monkeypatch.setattr(settings, "api_keys", ["k1"])
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/ws/goodsession") as wsx:
            wsx.receive_text()
    assert ei.value.code == 4003


def test_ws_bad_token_closes_4003(client, monkeypatch, fake_orch):
    monkeypatch.setattr(settings, "api_keys", ["k1"])
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/ws/goodsession?token=wrong") as wsx:
            wsx.receive_text()
    assert ei.value.code == 4003


def test_ws_good_token_connects(client, monkeypatch, fake_orch):
    monkeypatch.setattr(settings, "api_keys", ["k1"])
    # Connecting with a valid token should NOT raise on entry.
    with client.websocket_connect("/ws/goodsession?token=k1") as wsx:
        assert wsx is not None


def test_ws_auth_off_connects_without_token(client, fake_orch):
    # api_keys=[] -> verify_ws_token returns True; connect succeeds.
    with client.websocket_connect("/ws/goodsession") as wsx:
        assert wsx is not None


# --------------------------------------------------------------------------- #
# 6. Rate limiter -> 429
# --------------------------------------------------------------------------- #

def test_rate_limiter_returns_429_after_budget(client, fake_orch, monkeypatch):
    """Low rpm -> first N requests pass, then 429. Uses the shared singleton the
    routes actually call (auth.rate_limiter)."""
    auth_mod.rate_limiter.rpm = 2
    auth_mod.rate_limiter._windows.clear()

    codes = []
    for _ in range(4):
        r = client.post("/api/execute", json={"code": "result = 1"})
        codes.append(r.status_code)

    assert codes[:2] == [200, 200]
    assert codes[2] == 429
    assert codes[3] == 429


def test_rate_limiter_disabled_when_rpm_zero(client, fake_orch, monkeypatch):
    """rpm <= 0 disables the limiter (check() short-circuits) -> no 429."""
    auth_mod.rate_limiter.rpm = 0
    auth_mod.rate_limiter._windows.clear()
    codes = [
        client.post("/api/execute", json={"code": "result = 1"}).status_code
        for _ in range(5)
    ]
    assert all(c == 200 for c in codes)
