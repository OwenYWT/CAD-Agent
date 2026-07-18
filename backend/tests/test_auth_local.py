"""Local auth system tests — the login/register/invite/token machinery added by
the 'Add local auth and admin invite management' change.

These run with auth ON (the `auth` marker opts out of the global auth-off fixture
in conftest). They are hermetic: a per-test temp auth.db + history.db, no SMS, no
network. They exercise the real storage + API layers via TestClient.

Coverage:
  - register via verification code and via invite code
  - password login, verification-code login, logout (revocation), refresh, reset
  - dev_code is hidden unless AUTH_DEV_EXPOSE_CODE is on
  - forged / tampered / cross-secret tokens are rejected (no stateless fallback)
  - revoked + expired session tokens are rejected
  - admin guard on invite endpoints; admin cannot be created via register/code flows
  - per-user history isolation
  - verification-code request throttle
  - invite expiry edge cases (naive timestamp does not 500)
  - startup auth-config safety gate
  - legacy users-table migration carries rows forward
"""
import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

import app.main as appmain
from app.config import settings
from app.api import login as login_mod
from app.services.sms import SmsDeliveryResult
from app.storage import auth as auth_store
from app.storage import history as history_store

pytestmark = pytest.mark.auth

SECRET = "test-secret-" + "x" * 40
DEFAULT_INVITES = [
    "CAD1-A7K9",
    "CAD2-M4Q8",
    "CAD3-Z6P2",
    "CAD4-H9R5",
    "CAD5-T2N7",
    "CAD6-W8L3",
    "CAD7-Q5X1",
    "CAD8-B3V6",
    "CAD9-J2Y4",
    "CAD0-S9D8",
]


@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    """Auth ON, real secret, isolated DBs, fresh connections + in-memory state."""
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "auth_token_secret", SECRET)
    monkeypatch.setattr(settings, "api_keys", [])
    monkeypatch.setattr(settings, "auth_code_flows_enabled", True)
    monkeypatch.setattr(settings, "auth_dev_expose_code", True)  # so tests can read codes
    monkeypatch.setattr(settings, "admin_password", "")          # no admin unless a test sets it
    monkeypatch.setattr(settings, "default_invite_code", "")     # no seeded invite unless a test sets it
    monkeypatch.setattr(settings, "default_invite_codes", [])     # no seeded invites unless a test sets them
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    monkeypatch.setattr(settings, "verification_code_ttl_minutes", 10)

    # Fresh DB connections bound to the tmp path.
    asyncio.run(auth_store.close_db())
    asyncio.run(history_store.close_db())
    # Reset the per-process ephemeral secret + login throttle so tests don't bleed.
    auth_store._EPHEMERAL_SECRET = None
    login_mod._failed_logins.clear()
    yield tmp_path
    asyncio.run(auth_store.close_db())
    asyncio.run(history_store.close_db())


@pytest.fixture
def client(auth_env):
    # Do NOT run lifespan (it would hit the startup self-check / docker). The routes
    # we test don't need it; DB is lazily opened on first query.
    return TestClient(appmain.app)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

def _request_code(client, phone, purpose):
    r = client.post("/api/auth/code/request", json={"phone": phone, "purpose": purpose})
    assert r.status_code == 200, r.text
    return r.json()["dev_code"]


def test_register_with_code_then_login(client):
    phone = "13800000001"
    code = _request_code(client, phone, "register")
    r = client.post("/api/auth/register/code", json={"phone": phone, "code": code, "password": "secret123"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["phone"] == phone
    assert body["user"]["is_admin"] is False
    assert body["token"]

    # password login works afterwards
    r2 = client.post("/api/auth/login/password", json={"phone": phone, "password": "secret123"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["user"]["phone"] == phone


def test_register_with_invite(client, monkeypatch):
    monkeypatch.setattr(settings, "default_invite_codes", ["INVITE-OK"])
    monkeypatch.setattr(settings, "default_invite_max_uses", 2)
    phone = "13800000002"
    r = client.post("/api/auth/register/invite", json={"phone": phone, "invite_code": "INVITE-OK", "password": "secret123"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["registered_via"] == "invite_code"


def test_register_with_bad_invite_rejected(client):
    r = client.post("/api/auth/register/invite", json={"phone": "13800000003", "invite_code": "NOPE", "password": "secret123"})
    assert r.status_code == 400


def test_register_duplicate_phone_rejected(client):
    phone = "13800000004"
    code = _request_code(client, phone, "register")
    assert client.post("/api/auth/register/code", json={"phone": phone, "code": code, "password": "secret123"}).status_code == 200
    code2 = client.post("/api/auth/code/request", json={"phone": phone, "purpose": "register"})
    # register purpose for an existing phone still issues a code (no enumeration),
    # but completing registration fails.
    assert code2.status_code == 200
    dev = code2.json()["dev_code"]
    r = client.post("/api/auth/register/code", json={"phone": phone, "code": dev, "password": "secret123"})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Login / logout / refresh / me
# --------------------------------------------------------------------------- #

def _register(client, phone="13800000010", password="secret123"):
    code = _request_code(client, phone, "register")
    r = client.post("/api/auth/register/code", json={"phone": phone, "code": code, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def test_wrong_password_rejected(client):
    _register(client, "13800000011")
    r = client.post("/api/auth/login/password", json={"phone": "13800000011", "password": "wrongpass"})
    assert r.status_code == 400


def test_me_requires_token(client):
    assert client.get("/api/auth/me").status_code == 401
    sess = _register(client, "13800000012")
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {sess['token']}"})
    assert r.status_code == 200
    assert r.json()["user"]["phone"] == "13800000012"


def test_logout_revokes_token(client):
    sess = _register(client, "13800000013")
    headers = {"Authorization": f"Bearer {sess['token']}"}
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    # Token must no longer be accepted after logout.
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_refresh_rotates_and_old_token_dies(client):
    sess = _register(client, "13800000014")
    old = sess["token"]
    r = client.post("/api/auth/refresh", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 200, r.text
    new = r.json()["token"]
    assert new != old
    # old token revoked
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {old}"}).status_code == 401
    # new token works
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {new}"}).status_code == 200


def test_login_with_code(client):
    phone = "13800000015"
    _register(client, phone)
    code = _request_code(client, phone, "login")
    r = client.post("/api/auth/login/code", json={"phone": phone, "code": code})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["phone"] == phone


def test_reset_password(client):
    phone = "13800000016"
    _register(client, phone, password="oldpassword")
    code = _request_code(client, phone, "reset_password")
    r = client.post("/api/auth/password/reset", json={"phone": phone, "code": code, "password": "newpassword"})
    assert r.status_code == 200, r.text
    assert client.post("/api/auth/login/password", json={"phone": phone, "password": "newpassword"}).status_code == 200
    assert client.post("/api/auth/login/password", json={"phone": phone, "password": "oldpassword"}).status_code == 400


# --------------------------------------------------------------------------- #
# Token forgery — the BLOCKER. With a known/empty secret a token must NOT verify,
# and a token whose token_id is absent from auth_sessions must be rejected.
# --------------------------------------------------------------------------- #

def test_forged_token_with_wrong_secret_rejected(client):
    # Build a token signed with a DIFFERENT secret; it must not verify.
    import base64, hmac, hashlib
    payload = "someuserid:9999999999:deadbeefdeadbeef"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(b"attacker-guess", encoded.encode(), hashlib.sha256).hexdigest()
    forged = f"{encoded}.{sig}"
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_validly_signed_but_unknown_session_rejected(client):
    # Even signed with the REAL secret, a token whose token_id is not in
    # auth_sessions must be rejected (no stateless fallback).
    import base64, hmac, hashlib
    payload = "ghostuser:9999999999:0123456789abcdef"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    token = f"{encoded}.{sig}"
    assert asyncio.run(auth_store.verify_session_token(token)) is None


def test_user_id_is_not_derived_from_phone(client):
    sess = _register(client, "13800000017")
    import hashlib
    guessable = hashlib.sha256(b"phone:13800000017").hexdigest()[:24]
    assert sess["user"]["id"] != guessable
    assert len(sess["user"]["id"]) >= 24


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #

def test_admin_cannot_be_registered(client):
    # "admin" must never become a self-registered account. The code-request schema
    # (phone min_length=6) rejects it at 422; and the store guard rejects it too.
    code_resp = client.post("/api/auth/code/request", json={"phone": "admin", "purpose": "register"})
    assert code_resp.status_code in (400, 422)
    # Direct store-level guards: admin is refused in both register and code flows.
    with pytest.raises(ValueError):
        asyncio.run(auth_store.create_user("admin", "secret123", "verification_code"))
    with pytest.raises(ValueError):
        asyncio.run(auth_store.issue_verification_code("admin", "register"))


def test_invite_endpoints_require_admin(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "admin-strong-pw")
    asyncio.run(auth_store.ensure_admin_user())
    # a normal user cannot list invites
    sess = _register(client, "13800000018")
    r = client.get("/api/auth/invites", headers={"Authorization": f"Bearer {sess['token']}"})
    assert r.status_code == 403
    # admin can
    admin_login = client.post("/api/auth/login/password", json={"phone": "admin", "password": "admin-strong-pw"})
    assert admin_login.status_code == 200, admin_login.text
    atoken = admin_login.json()["token"]
    assert client.get("/api/auth/invites", headers={"Authorization": f"Bearer {atoken}"}).status_code == 200
    # admin can create an invite
    r = client.post("/api/auth/invites", json={"code": "NEWCODE", "max_uses": 3},
                    headers={"Authorization": f"Bearer {atoken}"})
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "NEWCODE"


def test_admin_not_created_without_password(client):
    # admin_password is "" in auth_env, so ensure_admin_user is a no-op.
    asyncio.run(auth_store.ensure_admin_user())
    r = client.post("/api/auth/login/password", json={"phone": "admin", "password": "anything12"})
    assert r.status_code == 400


def test_invite_only_mode_blocks_code_endpoints(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_code_flows_enabled", False)
    assert client.post("/api/auth/code/request", json={"phone": "13800000051", "purpose": "register"}).status_code == 403
    assert client.post(
        "/api/auth/register/code",
        json={"phone": "13800000051", "code": "123456", "password": "secret123"},
    ).status_code == 403
    assert client.post(
        "/api/auth/login/code",
        json={"phone": "13800000051", "code": "123456"},
    ).status_code == 403
    assert client.post(
        "/api/auth/password/reset",
        json={"phone": "13800000051", "code": "123456", "password": "secret123"},
    ).status_code == 403


def test_default_private_beta_invites_seeded_single_use(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_code_flows_enabled", False)
    monkeypatch.setattr(settings, "default_invite_codes", DEFAULT_INVITES)
    monkeypatch.setattr(settings, "default_invite_code", "OLD-CODE")
    monkeypatch.setattr(settings, "default_invite_max_uses", 1)
    first = DEFAULT_INVITES[0]

    seeded = asyncio.run(auth_store.list_invite_codes())
    seeded_codes = {row["code"] for row in seeded}
    assert set(DEFAULT_INVITES).issubset(seeded_codes)

    r = client.post(
        "/api/auth/register/invite",
        json={"phone": "13800000052", "invite_code": first, "password": "secret123"},
    )
    assert r.status_code == 200, r.text
    reused = client.post(
        "/api/auth/register/invite",
        json={"phone": "13800000053", "invite_code": first, "password": "secret123"},
    )
    assert reused.status_code == 400
    random_code = client.post(
        "/api/auth/register/invite",
        json={"phone": "13800000054", "invite_code": "NOPE-0000", "password": "secret123"},
    )
    assert random_code.status_code == 400
    old_legacy_code = client.post(
        "/api/auth/register/invite",
        json={"phone": "13800000055", "invite_code": "OLD-CODE", "password": "secret123"},
    )
    assert old_legacy_code.status_code == 400


# --------------------------------------------------------------------------- #
# dev_code gating
# --------------------------------------------------------------------------- #

def test_code_request_requires_sms_when_dev_code_hidden(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_dev_expose_code", False)
    r = client.post("/api/auth/code/request", json={"phone": "13800000019", "purpose": "register"})
    assert r.status_code == 503
    assert "dev_code" not in r.json()
    assert "短信服务" in r.json()["detail"]


def test_code_request_sends_sms_when_dev_code_hidden(client, monkeypatch):
    sent = []

    async def fake_send(phone: str, code: str, purpose: str):
        sent.append((phone, code, purpose))
        return SmsDeliveryResult(provider="test-sms", message_id="msg-1")

    monkeypatch.setattr(settings, "auth_dev_expose_code", False)
    monkeypatch.setattr(login_mod, "send_verification_code_sms", fake_send)
    phone = "13800000039"
    r = client.post("/api/auth/code/request", json={"phone": phone, "purpose": "register"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "dev_code" not in body
    assert body["delivery_provider"] == "test-sms"
    assert body["message"] == "验证码已通过短信发送，请查收。"
    assert sent and sent[0][0] == phone and sent[0][2] == "register"
    # The delivered code is the only code that can complete registration.
    reg = client.post("/api/auth/register/code", json={"phone": phone, "code": sent[0][1], "password": "secret123"})
    assert reg.status_code == 200, reg.text


# --------------------------------------------------------------------------- #
# Verification-code throttle
# --------------------------------------------------------------------------- #

def test_verification_code_request_throttled(client):
    phone = "13800000020"
    last = None
    for _ in range(6):
        last = client.post("/api/auth/code/request", json={"phone": phone, "purpose": "register"})
    assert last.status_code == 429


# --------------------------------------------------------------------------- #
# Per-user history isolation
# --------------------------------------------------------------------------- #

def test_history_isolated_per_user(client):
    a = _register(client, "13800000021")
    b = _register(client, "13800000022")
    # user A seeds a session owned by A
    asyncio.run(history_store.create_session("sess-A", title="A", user_id=a["user"]["id"]))
    # A sees it, B does not
    ra = client.get("/api/history/sessions", headers={"Authorization": f"Bearer {a['token']}"})
    rb = client.get("/api/history/sessions", headers={"Authorization": f"Bearer {b['token']}"})
    assert any(s["id"] == "sess-A" for s in ra.json())
    assert all(s["id"] != "sess-A" for s in rb.json())
    # B cannot read A's panels (404, not data)
    assert client.get("/api/history/sessions/sess-A/panels",
                      headers={"Authorization": f"Bearer {b['token']}"}).status_code == 404


# --------------------------------------------------------------------------- #
# Invite expiry edge cases
# --------------------------------------------------------------------------- #

def test_invite_with_naive_expiry_does_not_500(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "admin-strong-pw")
    asyncio.run(auth_store.ensure_admin_user())
    atoken = client.post("/api/auth/login/password",
                         json={"phone": "admin", "password": "admin-strong-pw"}).json()["token"]
    # naive (no tz) future timestamp — must be accepted/normalized, never crash
    r = client.post("/api/auth/invites",
                    json={"code": "FUTURE", "max_uses": 1, "expires_at": "2999-01-01T00:00:00"},
                    headers={"Authorization": f"Bearer {atoken}"})
    assert r.status_code == 200, r.text
    # consuming it must not raise (the naive/aware comparison bug)
    reg = client.post("/api/auth/register/invite",
                      json={"phone": "13800000023", "invite_code": "FUTURE", "password": "secret123"})
    assert reg.status_code == 200, reg.text


def test_invite_malformed_expiry_rejected_422(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "admin-strong-pw")
    asyncio.run(auth_store.ensure_admin_user())
    atoken = client.post("/api/auth/login/password",
                         json={"phone": "admin", "password": "admin-strong-pw"}).json()["token"]
    r = client.post("/api/auth/invites",
                    json={"code": "BADTIME", "max_uses": 1, "expires_at": "not-a-timestamp"},
                    headers={"Authorization": f"Bearer {atoken}"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Config safety gate
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("secret", ["", "change-me-in-production", "cad-agent-dev-secret"])
def test_auth_config_gate_rejects_insecure_secret(secret):
    from app.config import Settings
    s = Settings(_env_file=None, auth_required=True, auth_token_secret=secret)
    with pytest.raises(RuntimeError):
        s.assert_auth_config_safe()


def test_auth_config_gate_allows_good_secret():
    from app.config import Settings
    s = Settings(
        _env_file=None,
        auth_required=True,
        auth_token_secret="x" * 50,
        sms_provider="webhook",
        sms_webhook_url="https://sms.example.test/send",
    )
    s.assert_auth_config_safe()  # no raise


def test_auth_config_gate_skipped_when_auth_off():
    from app.config import Settings
    s = Settings(_env_file=None, auth_required=False, auth_token_secret="")
    s.assert_auth_config_safe()  # no raise


def test_dev_expose_code_blocked_in_prod_gate():
    from app.config import Settings
    s = Settings(
        _env_file=None,
        auth_required=True,
        auth_token_secret="x" * 50,
        auth_dev_expose_code=True,
        sms_provider="webhook",
        sms_webhook_url="https://sms.example.test/send",
    )
    with pytest.raises(RuntimeError):
        s.assert_auth_config_safe()


def test_auth_config_gate_rejects_missing_sms_provider():
    from app.config import Settings
    s = Settings(
        _env_file=None,
        auth_required=True,
        auth_token_secret="x" * 50,
        auth_code_flows_enabled=True,
        sms_provider="disabled",
    )
    with pytest.raises(RuntimeError):
        s.assert_auth_config_safe()


def test_auth_config_gate_rejects_incomplete_tencent_sms():
    from app.config import Settings
    s = Settings(
        _env_file=None,
        auth_required=True,
        auth_token_secret="x" * 50,
        auth_code_flows_enabled=True,
        sms_provider="tencentcloud",
    )
    with pytest.raises(RuntimeError):
        s.assert_auth_config_safe()


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #

def test_password_is_pbkdf2_not_plaintext():
    h = auth_store.hash_password("hunter2pw")
    assert h.startswith("pbkdf2_sha256$")
    assert "hunter2pw" not in h
    assert auth_store.verify_password("hunter2pw", h) is True
    assert auth_store.verify_password("wrong", h) is False


# --------------------------------------------------------------------------- #
# Legacy migration: a pre-existing users table keyed on "identifier" must carry
# its rows forward into the new phone-keyed schema (no silent data loss).
# --------------------------------------------------------------------------- #

def test_legacy_users_migration_preserves_rows(auth_env):
    import aiosqlite

    async def _run():
        db_path = auth_store._db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Build a legacy-schema users table (identifier column, no phone column).
        legacy = await aiosqlite.connect(str(db_path))
        await legacy.execute(
            """CREATE TABLE users (
                   id TEXT PRIMARY KEY,
                   identifier TEXT,
                   password_hash TEXT,
                   created_at TEXT,
                   last_login_at TEXT
               )"""
        )
        await legacy.execute(
            "INSERT INTO users (id, identifier, password_hash, created_at, last_login_at) VALUES (?,?,?,?,?)",
            ("uid-legacy-1", "13900000001", "pbkdf2_sha256$x", "2024-01-01T00:00:00+00:00", "2024-01-02T00:00:00+00:00"),
        )
        await legacy.commit()
        await legacy.close()

        # Opening via the app's get_db() triggers _migrate_legacy_tables.
        auth_store._db = None
        db = await auth_store.get_db()
        rows = await db.execute_fetchall("SELECT id, phone, password_hash FROM users")
        return [dict(r) for r in rows]

    rows = asyncio.run(_run())
    assert any(r["id"] == "uid-legacy-1" and r["phone"] == "13900000001" for r in rows), rows


# --------------------------------------------------------------------------- #
# WebSocket session ownership (P1): a logged-in user must not attach to a
# session_id already owned by another user.
# --------------------------------------------------------------------------- #

def test_ws_rejects_session_owned_by_other_user(client):
    import app.api.websocket as ws_mod
    ws_mod.sessions.clear()
    a = _register(client, "13800000031")
    b = _register(client, "13800000032")
    # A owns sess-owned-by-A
    asyncio.run(history_store.create_session("sess-owned-by-A", title="A", user_id=a["user"]["id"]))
    # B tries to attach to A's session over WS -> handshake refused (close 4003).
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/sess-owned-by-A?token={b['token']}") as wsk:
            wsk.receive_json()


def test_ws_allows_owner_and_new_session(client):
    import app.api.websocket as ws_mod
    ws_mod.sessions.clear()
    a = _register(client, "13800000033")
    # A attaches to a brand-new session id (no row yet) -> allowed (no disconnect at
    # handshake). We send nothing and close immediately; reaching the body is enough.
    with client.websocket_connect(f"/ws/sess-brand-new?token={a['token']}"):
        pass
    # A attaches to its OWN existing session -> allowed.
    asyncio.run(history_store.create_session("sess-mine", title="mine", user_id=a["user"]["id"]))
    with client.websocket_connect(f"/ws/sess-mine?token={a['token']}"):
        pass


def test_ws_writable_helper_semantics(auth_env):
    async def _run():
        await history_store.create_session("owned", user_id="user-A")
        await history_store.create_session("nullowner", user_id=None)
        return (
            await history_store.session_writable_by_user("does-not-exist", "user-A"),  # new -> True
            await history_store.session_writable_by_user("owned", "user-A"),            # own  -> True
            await history_store.session_writable_by_user("owned", "user-B"),            # other-> False
            await history_store.session_writable_by_user("nullowner", "user-B"),        # null -> True (claimable)
            await history_store.session_writable_by_user("owned", None),                # dev  -> True
        )
    new_ok, own_ok, other_blocked, null_ok, dev_ok = asyncio.run(_run())
    assert new_ok is True
    assert own_ok is True
    assert other_blocked is False
    assert null_ok is True
    assert dev_ok is True


# --------------------------------------------------------------------------- #
# Atomic consumption under concurrency (P1).
# --------------------------------------------------------------------------- #

def test_verification_code_consumed_once_under_concurrency(client):
    phone = "13800000041"
    _register(client, phone)  # ensure phone exists so a login code can be issued
    code = _request_code(client, phone, "login")

    async def _race():
        # Open the DB once so both coroutines share the same connection (mirrors the
        # single global aiosqlite connection used in-process).
        await auth_store.get_db()
        results = await asyncio.gather(
            auth_store.consume_verification_code(phone, "login", code),
            auth_store.consume_verification_code(phone, "login", code),
        )
        return results

    results = asyncio.run(_race())
    assert sorted(results) == [False, True], f"a code must be consumable exactly once, got {results}"


def test_invite_code_max_uses_atomic_under_concurrency(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "admin-strong-pw")
    asyncio.run(auth_store.ensure_admin_user())
    atoken = client.post("/api/auth/login/password",
                         json={"phone": "admin", "password": "admin-strong-pw"}).json()["token"]
    # single-use invite
    r = client.post("/api/auth/invites", json={"code": "ONESHOT", "max_uses": 1},
                    headers={"Authorization": f"Bearer {atoken}"})
    assert r.status_code == 200, r.text

    async def _race():
        await auth_store.get_db()
        return await asyncio.gather(
            auth_store.consume_invite_code("ONESHOT"),
            auth_store.consume_invite_code("ONESHOT"),
            auth_store.consume_invite_code("ONESHOT"),
        )

    results = asyncio.run(_race())
    assert sum(1 for x in results if x) == 1, f"max_uses=1 must allow exactly one use, got {results}"
    # used_count must not exceed max_uses
    info = asyncio.run(auth_store.get_invite_code("ONESHOT"))
    assert info["used_count"] == 1, info
