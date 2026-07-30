import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from app.config import settings

_db: aiosqlite.Connection | None = None
_PHONE_RE = re.compile(r"^\+?\d{6,20}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_text() -> str:
    return _now().isoformat()


def _parse_dt(value: str) -> datetime:
    """Parse a stored ISO timestamp, coercing naive values to UTC so comparisons
    against the tz-aware _now() never raise 'can't compare offset-naive and
    offset-aware datetimes'. Used for any stored expires_at that may have been
    supplied by a client (e.g. invite expires_at)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _db_path() -> Path:
    history_path = Path(settings.history_db_path)
    return history_path.with_name("auth.db")


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is not None:
        return _db
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(str(path))
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _init_tables(_db)
    return _db


async def close_db():
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def _init_tables(db: aiosqlite.Connection):
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            phone TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            registered_via TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_login_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS verification_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            purpose TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invite_codes (
            code TEXT PRIMARY KEY,
            max_uses INTEGER NOT NULL DEFAULT 1,
            used_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            disabled_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            created_at TEXT NOT NULL
        );
    """)
    await _migrate_legacy_tables(db)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_verification_phone ON verification_codes(phone, purpose, expires_at)"
    )
    await db.commit()


async def _migrate_legacy_tables(db: aiosqlite.Connection):
    user_columns = await db.execute_fetchall("PRAGMA table_info(users)")
    user_column_names = {row["name"] for row in user_columns}
    if "identifier" in user_column_names and "phone" not in user_column_names:
        await db.execute("ALTER TABLE users RENAME TO users_legacy")
        await db.executescript("""
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                phone TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                registered_via TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_login_at TEXT NOT NULL
            );
        """)
        # Carry existing accounts forward instead of abandoning them in the
        # renamed table (the old code created an empty table => silent data loss).
        # The legacy "identifier" column maps onto the new "phone" column. Rows that
        # violate NOT NULL/UNIQUE are skipped (INSERT OR IGNORE) rather than aborting.
        legacy_cols = {row["name"] for row in await db.execute_fetchall("PRAGMA table_info(users_legacy)")}
        if {"id", "identifier", "password_hash"}.issubset(legacy_cols):
            reg_expr = "registered_via" if "registered_via" in legacy_cols else "'legacy'"
            created_src = "created_at" if "created_at" in legacy_cols else "NULL"
            login_src = "last_login_at" if "last_login_at" in legacy_cols else "NULL"
            await db.execute(f"""
                INSERT OR IGNORE INTO users (id, phone, password_hash, registered_via, created_at, last_login_at)
                SELECT id, identifier, password_hash, {reg_expr},
                       COALESCE({created_src}, ?), COALESCE({login_src}, ?)
                FROM users_legacy
            """, (_now_text(), _now_text()))
    else:
        if "phone" not in user_column_names:
            await db.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        if "password_hash" not in user_column_names:
            await db.execute("ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")
        if "registered_via" not in user_column_names:
            await db.execute("ALTER TABLE users ADD COLUMN registered_via TEXT NOT NULL DEFAULT 'legacy'")

    invite_columns = await db.execute_fetchall("PRAGMA table_info(invite_codes)")
    invite_column_names = {row["name"] for row in invite_columns}
    if "disabled_at" not in invite_column_names:
        await db.execute("ALTER TABLE invite_codes ADD COLUMN disabled_at TEXT")

    code_columns = await db.execute_fetchall("PRAGMA table_info(verification_codes)")
    code_column_names = {row["name"] for row in code_columns}
    if "identifier" in code_column_names and "phone" not in code_column_names:
        await db.execute("ALTER TABLE verification_codes RENAME TO verification_codes_legacy")
        await db.executescript("""
            CREATE TABLE verification_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                purpose TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_verification_phone ON verification_codes(phone, purpose, expires_at);
        """)
    else:
        if "phone" not in code_column_names:
            await db.execute("ALTER TABLE verification_codes ADD COLUMN phone TEXT")
        if "purpose" not in code_column_names:
            await db.execute("ALTER TABLE verification_codes ADD COLUMN purpose TEXT NOT NULL DEFAULT 'login'")


def normalize_phone(phone: str) -> str:
    normalized = re.sub(r"[\s-]", "", phone.strip())
    if normalized.lower() == "admin":
        return "admin"
    if not _PHONE_RE.match(normalized):
        raise ValueError("请输入有效手机号")
    return normalized


def is_admin_account(account: str) -> bool:
    return account.strip().lower() == "admin"


def validate_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("密码至少需要 8 位")
    if len(password) > 128:
        raise ValueError("密码不能超过 128 位")
    return password


def _secret() -> str:
    secret = (settings.auth_token_secret or "").strip()
    if not secret:
        # No usable signing key. When auth is required this is caught at startup
        # (assert_auth_config_safe); reaching here means auth is disabled OR a test
        # forgot to set one. Use a per-process random key so tokens are at least
        # unforgeable from outside this process (and useless across restarts),
        # never the old public "cad-agent-dev-secret" constant.
        global _EPHEMERAL_SECRET
        if _EPHEMERAL_SECRET is None:
            _EPHEMERAL_SECRET = secrets.token_urlsafe(48)
        return _EPHEMERAL_SECRET
    return secret


_EPHEMERAL_SECRET: str | None = None


def _hash_value(value: str) -> str:
    return hmac.new(_secret().encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    password = validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return "pbkdf2_sha256$200000$" + base64.urlsafe_b64encode(salt).decode("ascii") + "$" + base64.urlsafe_b64encode(digest).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations_text))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _sign(payload: str) -> str:
    return hmac.new(_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _make_token(user_id: str, token_id: str, expires_at: datetime) -> str:
    payload = f"{user_id}:{int(expires_at.timestamp())}:{token_id}"
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{encoded}.{_sign(encoded)}"


async def create_session_token(user_id: str, ttl_hours: int | None = None) -> str:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.create_session_token(user_id, ttl_hours)
    expires_at = _now() + timedelta(hours=ttl_hours or settings.auth_token_ttl_hours)
    token_id = secrets.token_hex(8)
    token = _make_token(user_id, token_id, expires_at)
    db = await get_db()
    await db.execute(
        "INSERT INTO auth_sessions (token_id, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (token_id, user_id, expires_at.isoformat(), _now_text()),
    )
    await db.commit()
    return token


def parse_access_token(token: str | None) -> tuple[str, int, str] | None:
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(encoded), signature):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        user_id, expires_at_text, token_id = payload.split(":", 2)
        expires_at = int(expires_at_text)
        if expires_at < int(_now().timestamp()):
            return None
        return user_id, expires_at, token_id
    except Exception:
        return None


async def verify_session_token(token: str | None) -> str | None:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.verify_session_token(token)
    parsed = parse_access_token(token)
    if not parsed:
        return None
    user_id, _expires_at, token_id = parsed
    db = await get_db()
    cursor = await db.execute(
        "SELECT user_id, expires_at, revoked_at FROM auth_sessions WHERE token_id = ?",
        (token_id,),
    )
    row = await cursor.fetchone()
    if not row:
        # A validly-signed token whose token_id is not in auth_sessions is rejected.
        # (Previously this branch returned user_id, which made every signed token
        # un-revocable and let a forged token_id bypass the session table entirely.)
        return None
    if row["revoked_at"]:
        return None
    if _parse_dt(row["expires_at"]) < _now():
        return None
    return row["user_id"]


async def revoke_session_token(token: str | None):
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.revoke_session_token(token)
    parsed = parse_access_token(token)
    if not parsed:
        return
    _user_id, _expires_at, token_id = parsed
    db = await get_db()
    await db.execute("UPDATE auth_sessions SET revoked_at = ? WHERE token_id = ?", (_now_text(), token_id))
    await db.commit()


async def refresh_session_token(token: str | None) -> str | None:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.refresh_session_token(token)
    user_id = await verify_session_token(token)
    if not user_id:
        return None
    await revoke_session_token(token)
    return await create_session_token(user_id)


def public_user(row: aiosqlite.Row | dict) -> dict:
    phone = row["phone"]
    return {
        "id": row["id"],
        "phone": phone,
        "registered_via": row["registered_via"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
        "is_admin": is_admin_account(phone),
    }


async def ensure_admin_user():
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.ensure_admin_user()
    # No admin password configured => do NOT auto-create an admin. (Previously this
    # shipped a default "admin123456", an instant takeover of the invite-management
    # surface.) Operators must set ADMIN_PASSWORD explicitly to provision admin.
    if not settings.admin_password:
        return
    db = await get_db()
    now = _now_text()
    # INSERT OR IGNORE on the UNIQUE phone makes concurrent first-logins idempotent
    # (no double-insert race). The admin id stays the stable literal "admin".
    await db.execute(
        """
        INSERT OR IGNORE INTO users (id, phone, password_hash, registered_via, created_at, last_login_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("admin", "admin", hash_password(settings.admin_password), "admin", now, now),
    )
    await db.commit()


async def user_exists(phone: str) -> bool:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.user_exists(phone)
    normalized = normalize_phone(phone)
    db = await get_db()
    cursor = await db.execute("SELECT 1 FROM users WHERE phone = ?", (normalized,))
    return await cursor.fetchone() is not None


# Max verification codes a single phone+purpose may request inside the TTL window.
# Bounds the verification_codes table and blunts code-flooding / brute-prep.
_MAX_CODES_PER_WINDOW = 5


class VerificationThrottleError(Exception):
    """Raised when a phone requests verification codes too frequently."""


async def issue_verification_code(phone: str, purpose: str) -> str:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.issue_verification_code(phone, purpose)
    normalized = normalize_phone(phone)
    if purpose not in {"register", "login", "reset_password"}:
        raise ValueError("验证码用途无效")
    # The built-in admin account is managed by password only — never issue codes
    # for it (otherwise the dev-code / reset flow becomes an admin-takeover path).
    if is_admin_account(normalized):
        raise ValueError("管理员账号不支持验证码流程")
    exists = await user_exists(normalized)
    # NOTE: we intentionally do NOT branch the error on whether the phone is
    # registered (that leaks account existence). For register-on-existing and
    # login/reset-on-missing we still proceed to issue a code; the downstream
    # register/login/reset step returns a uniform failure. The only signal a
    # caller gets here is throttling.
    _ = exists

    # Per-phone+purpose request throttle within the active TTL window.
    window_start = (_now() - timedelta(minutes=settings.verification_code_ttl_minutes)).isoformat()
    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) AS n FROM verification_codes WHERE phone = ? AND purpose = ? AND created_at >= ?",
        (normalized, purpose, window_start),
    )
    recent = await cursor.fetchone()
    if recent and recent["n"] >= _MAX_CODES_PER_WINDOW:
        raise VerificationThrottleError("验证码请求过于频繁，请稍后再试")

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = (_now() + timedelta(minutes=settings.verification_code_ttl_minutes)).isoformat()
    await db.execute(
        "INSERT INTO verification_codes (phone, purpose, code_hash, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
        (normalized, purpose, _hash_value(code), expires_at, _now_text()),
    )
    # Opportunistic cleanup of expired/consumed rows for this phone so the table
    # doesn't grow without bound.
    await db.execute(
        "DELETE FROM verification_codes WHERE phone = ? AND (expires_at < ? OR consumed_at IS NOT NULL)",
        (normalized, _now_text()),
    )
    await db.commit()
    return code


async def delete_verification_code(phone: str, purpose: str, code: str) -> None:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.delete_verification_code(
            phone,
            purpose,
            code,
        )
    normalized = normalize_phone(phone)
    db = await get_db()
    await db.execute(
        "DELETE FROM verification_codes WHERE phone = ? AND purpose = ? AND code_hash = ? AND consumed_at IS NULL",
        (normalized, purpose, _hash_value(code)),
    )
    await db.commit()


async def consume_verification_code(phone: str, purpose: str, code: str) -> bool:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.consume_verification_code(
            phone,
            purpose,
            code,
        )
    normalized = normalize_phone(phone)
    db = await get_db()
    rows = await db.execute_fetchall(
        """
        SELECT id, code_hash, expires_at, consumed_at
        FROM verification_codes
        WHERE phone = ? AND purpose = ?
        ORDER BY id DESC
        LIMIT 5
        """,
        (normalized, purpose),
    )
    now = _now()
    for row in rows:
        if row["consumed_at"]:
            continue
        expires_at = _parse_dt(row["expires_at"])
        if expires_at < now:
            continue
        if hmac.compare_digest(row["code_hash"], _hash_value(code.strip())):
            # Atomic claim: the `AND consumed_at IS NULL` guard means only one
            # concurrent consumer can flip the row; cursor.rowcount tells us whether
            # WE were the one that claimed it. (Previously the UPDATE was
            # unconditional, so two requests racing on the same code both "won".)
            cursor = await db.execute(
                "UPDATE verification_codes SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (_now_text(), row["id"]),
            )
            await db.commit()
            if cursor.rowcount == 1:
                return True
            # Lost the race for this row — keep scanning for another valid code.
            continue
    return False


def _seed_invite_codes() -> list[str]:
    codes: list[str] = []
    for code in settings.default_invite_codes:
        normalized = code.strip().upper()
        if normalized and normalized not in codes:
            codes.append(normalized)
    if codes:
        return codes
    legacy = settings.default_invite_code.strip().upper()
    if legacy and legacy not in codes:
        codes.append(legacy)
    return codes


async def ensure_default_invite_code():
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.ensure_default_invite_code()
    codes = _seed_invite_codes()
    if not codes:
        return
    db = await get_db()
    now = _now_text()
    for code in codes:
        await db.execute(
            "INSERT OR IGNORE INTO invite_codes (code, max_uses, used_count, expires_at, disabled_at, created_at) VALUES (?, ?, 0, NULL, NULL, ?)",
            (code, settings.default_invite_max_uses, now),
        )
    await db.commit()


async def consume_invite_code(code: str) -> bool:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.consume_invite_code(code)
    await ensure_default_invite_code()
    normalized = code.strip().upper()
    if not normalized:
        return False
    allowed = _seed_invite_codes()
    if allowed and normalized not in allowed:
        return False
    db = await get_db()
    # Pre-check expiry separately: SQLite can't reliably compare arbitrary ISO
    # strings, and expiry is time- not count-based, so it's safe to read first.
    cursor = await db.execute(
        "SELECT expires_at FROM invite_codes WHERE code = ?",
        (normalized,),
    )
    row = await cursor.fetchone()
    if not row:
        return False
    if row["expires_at"] and _parse_dt(row["expires_at"]) < _now():
        return False
    # Atomic increment: the WHERE clause enforces the seat limit and not-disabled
    # condition inside the single UPDATE, so two concurrent registrations on the
    # last remaining use can't both succeed. rowcount == 1 means we claimed a seat.
    cursor = await db.execute(
        """
        UPDATE invite_codes
        SET used_count = used_count + 1
        WHERE code = ? AND disabled_at IS NULL AND used_count < max_uses
        """,
        (normalized,),
    )
    await db.commit()
    return cursor.rowcount == 1


async def create_invite_code(code: str | None, max_uses: int, expires_at: str | None = None) -> dict:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.create_invite_code(
            code,
            max_uses,
            expires_at,
        )
    normalized = (code or secrets.token_urlsafe(8)).strip()
    if len(normalized) < 3:
        raise ValueError("邀请码至少需要 3 个字符")
    if max_uses < 1:
        raise ValueError("最大使用次数至少为 1")
    db = await get_db()
    await db.execute(
        "INSERT INTO invite_codes (code, max_uses, used_count, expires_at, disabled_at, created_at) VALUES (?, ?, 0, ?, NULL, ?)",
        (normalized, max_uses, expires_at, _now_text()),
    )
    await db.commit()
    return await get_invite_code(normalized)


async def list_invite_codes() -> list[dict]:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.list_invite_codes()
    await ensure_default_invite_code()
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT code, max_uses, used_count, expires_at, disabled_at, created_at FROM invite_codes ORDER BY created_at DESC LIMIT 100"
    )
    return [dict(row) for row in rows]


async def get_invite_code(code: str) -> dict:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.get_invite_code(code)
    db = await get_db()
    cursor = await db.execute(
        "SELECT code, max_uses, used_count, expires_at, disabled_at, created_at FROM invite_codes WHERE code = ?",
        (code,),
    )
    row = await cursor.fetchone()
    if not row:
        raise ValueError("邀请码不存在")
    return dict(row)


async def disable_invite_code(code: str):
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.disable_invite_code(code)
    db = await get_db()
    await db.execute("UPDATE invite_codes SET disabled_at = ? WHERE code = ?", (_now_text(), code))
    await db.commit()


async def create_user(phone: str, password: str, registered_via: str) -> dict:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.create_user(
            phone,
            password,
            registered_via,
        )
    normalized = normalize_phone(phone)
    if normalized == "admin":
        raise ValueError("管理员账号不能通过注册创建")
    if await user_exists(normalized):
        raise ValueError("该手机号已注册，请直接登录")
    password_hash = hash_password(password)
    # Random, non-guessable id. (Previously sha256("phone:"+phone)[:24], which made
    # every user's id derivable from their phone number — combined with a leaked
    # signing key that turned into trivial token forgery.)
    user_id = secrets.token_hex(16)
    now = _now_text()
    db = await get_db()
    await db.execute(
        """
        INSERT INTO users (id, phone, password_hash, registered_via, created_at, last_login_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, normalized, password_hash, registered_via, now, now),
    )
    await db.commit()
    user = await get_user(user_id)
    if user is None:
        raise RuntimeError("用户创建失败")
    return user


async def reset_password(phone: str, password: str) -> dict:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.reset_password(phone, password)
    normalized = normalize_phone(phone)
    if is_admin_account(normalized):
        raise ValueError("管理员账号不支持验证码流程")
    # Confirm existence BEFORE mutating, so we never run an UPDATE for a phone that
    # isn't registered (and the error path is deterministic).
    existing = await get_user_by_phone(normalized)
    if not existing:
        raise ValueError("该手机号尚未注册，请先注册")
    password_hash = hash_password(password)
    db = await get_db()
    await db.execute(
        "UPDATE users SET password_hash = ?, last_login_at = ? WHERE phone = ?",
        (password_hash, _now_text(), normalized),
    )
    await db.commit()
    return await get_user_by_phone(normalized)


async def authenticate_password(phone: str, password: str) -> dict | None:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.authenticate_password(phone, password)
    normalized = normalize_phone(phone)
    if normalized == "admin":
        await ensure_admin_user()
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, phone, password_hash, registered_via, created_at, last_login_at FROM users WHERE phone = ?",
        (normalized,),
    )
    row = await cursor.fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    await touch_login(row["id"])
    return await get_user(row["id"])


async def get_user_by_phone(phone: str) -> dict | None:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.get_user_by_phone(phone)
    normalized = normalize_phone(phone)
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, phone, registered_via, created_at, last_login_at FROM users WHERE phone = ?",
        (normalized,),
    )
    row = await cursor.fetchone()
    return public_user(row) if row else None


async def touch_login(user_id: str):
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.touch_login(user_id)
    db = await get_db()
    await db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_now_text(), user_id))
    await db.commit()


async def get_user(user_id: str) -> dict | None:
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.get_user(user_id)
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, phone, registered_via, created_at, last_login_at FROM users WHERE id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()
    return public_user(row) if row else None


async def delete_user(user_id: str):
    if settings.durable_control_plane_enabled:
        from app.storage import postgres_auth

        return await postgres_auth.delete_user(user_id)
    user = await get_user(user_id)
    if user and user["is_admin"]:
        raise ValueError("管理员账号不能注销")
    db = await get_db()
    await db.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()
