"""PostgreSQL implementation of the legacy auth-store contract."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import auth_transaction
from app.domain.identity import platform_service_principal, user_principal
from app.repositories.identity import reconcile_principal


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _phone_hash(phone: str) -> str:
    return hashlib.sha256(phone.encode("utf-8")).hexdigest()


def _public(row: Any) -> dict:
    from app.storage.auth import is_admin_account

    return {
        "id": row["id"],
        "phone": row["phone"],
        "registered_via": row["registered_via"],
        "created_at": row["created_at"].isoformat(),
        "last_login_at": row["last_login_at"].isoformat(),
        "is_admin": is_admin_account(row["phone"]),
    }


async def _platform_context():
    return await reconcile_principal(
        platform_service_principal("authentication"),
        display_name="身份认证服务",
    )


async def create_session_token(user_id: str, ttl_hours: int | None = None) -> str:
    from app.storage.auth import _make_token

    expires_at = _now() + timedelta(
        hours=ttl_hours or settings.auth_token_ttl_hours
    )
    token_id = secrets.token_hex(8)
    async with auth_transaction() as connection:
        user = (
            await connection.execute(
                text("SELECT tenant_id FROM auth_users WHERE id=:id"),
                {"id": user_id},
            )
        ).mappings().one_or_none()
        if user is None:
            raise ValueError("用户不存在")
        await connection.execute(
            text(
                """
                INSERT INTO auth_sessions (
                    token_id, tenant_id, user_id, expires_at
                ) VALUES (:token, :tenant, :user, :expires)
                """
            ),
            {
                "token": token_id,
                "tenant": user["tenant_id"],
                "user": user_id,
                "expires": expires_at,
            },
        )
    return _make_token(user_id, token_id, expires_at)


async def verify_session_token(token: str | None) -> str | None:
    from app.storage.auth import parse_access_token

    parsed = parse_access_token(token)
    if not parsed:
        return None
    user_id, _, token_id = parsed
    async with auth_transaction() as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT user_id, expires_at, revoked_at
                    FROM auth_sessions WHERE token_id=:token
                    """
                ),
                {"token": token_id},
            )
        ).mappings().one_or_none()
    if (
        row is None
        or row["user_id"] != user_id
        or row["revoked_at"] is not None
        or row["expires_at"] < _now()
    ):
        return None
    return user_id


async def revoke_session_token(token: str | None) -> None:
    from app.storage.auth import parse_access_token

    parsed = parse_access_token(token)
    if not parsed:
        return
    async with auth_transaction() as connection:
        await connection.execute(
            text(
                """
                UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP
                WHERE token_id=:token AND revoked_at IS NULL
                """
            ),
            {"token": parsed[2]},
        )


async def refresh_session_token(token: str | None) -> str | None:
    user_id = await verify_session_token(token)
    if not user_id:
        return None
    await revoke_session_token(token)
    return await create_session_token(user_id)


async def ensure_admin_user() -> None:
    if not settings.admin_password:
        return
    from app.storage.auth import hash_password

    context = user_principal("admin")
    await reconcile_principal(context, display_name="admin")
    now = _now()
    async with auth_transaction() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO auth_users (
                    id, tenant_id, principal_id, phone, phone_lookup_hash,
                    password_hash, registered_via, created_at,
                    last_login_at, updated_at
                ) VALUES (
                    'admin', :tenant, :principal, 'admin', :phone_hash,
                    :password_hash, 'admin', :now, :now, :now
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "tenant": context.tenant_id,
                "principal": context.principal_id,
                "phone_hash": _phone_hash("admin"),
                "password_hash": hash_password(settings.admin_password),
                "now": now,
            },
        )


async def user_exists(phone: str) -> bool:
    from app.storage.auth import normalize_phone

    normalized = normalize_phone(phone)
    async with auth_transaction() as connection:
        return bool(
            await connection.scalar(
                text(
                    "SELECT 1 FROM auth_users "
                    "WHERE phone_lookup_hash=:phone_hash"
                ),
                {"phone_hash": _phone_hash(normalized)},
            )
        )


async def issue_verification_code(phone: str, purpose: str) -> str:
    from app.storage.auth import (
        VerificationThrottleError,
        _MAX_CODES_PER_WINDOW,
        _hash_value,
        is_admin_account,
        normalize_phone,
    )

    normalized = normalize_phone(phone)
    if purpose not in {"register", "login", "reset_password"}:
        raise ValueError("验证码用途无效")
    if is_admin_account(normalized):
        raise ValueError("管理员账号不支持验证码流程")
    context = await _platform_context()
    window_start = _now() - timedelta(
        minutes=settings.verification_code_ttl_minutes
    )
    async with auth_transaction() as connection:
        recent = await connection.scalar(
            text(
                """
                SELECT count(*) FROM auth_verification_codes
                WHERE phone_lookup_hash=:phone_hash
                  AND purpose=:purpose AND created_at >= :window_start
                """
            ),
            {
                "phone_hash": _phone_hash(normalized),
                "purpose": purpose,
                "window_start": window_start,
            },
        )
        if int(recent or 0) >= _MAX_CODES_PER_WINDOW:
            raise VerificationThrottleError(
                "验证码请求过于频繁，请稍后再试"
            )
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = _now()
        await connection.execute(
            text(
                """
                DELETE FROM auth_verification_codes
                WHERE phone_lookup_hash=:phone_hash
                  AND (expires_at < :now OR consumed_at IS NOT NULL)
                """
            ),
            {"phone_hash": _phone_hash(normalized), "now": now},
        )
        await connection.execute(
            text(
                """
                INSERT INTO auth_verification_codes (
                    tenant_id, phone_lookup_hash, purpose, code_hash,
                    expires_at, created_at
                ) VALUES (
                    :tenant, :phone_hash, :purpose, :code_hash,
                    :expires, :now
                )
                """
            ),
            {
                "tenant": context.tenant_id,
                "phone_hash": _phone_hash(normalized),
                "purpose": purpose,
                "code_hash": _hash_value(code),
                "expires": now
                + timedelta(minutes=settings.verification_code_ttl_minutes),
                "now": now,
            },
        )
    return code


async def delete_verification_code(
    phone: str,
    purpose: str,
    code: str,
) -> None:
    from app.storage.auth import _hash_value, normalize_phone

    normalized = normalize_phone(phone)
    async with auth_transaction() as connection:
        await connection.execute(
            text(
                """
                DELETE FROM auth_verification_codes
                WHERE phone_lookup_hash=:phone_hash AND purpose=:purpose
                  AND code_hash=:code_hash AND consumed_at IS NULL
                """
            ),
            {
                "phone_hash": _phone_hash(normalized),
                "purpose": purpose,
                "code_hash": _hash_value(code),
            },
        )


async def consume_verification_code(
    phone: str,
    purpose: str,
    code: str,
) -> bool:
    from app.storage.auth import _hash_value, normalize_phone

    normalized = normalize_phone(phone)
    async with auth_transaction() as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT id, code_hash, expires_at, consumed_at
                    FROM auth_verification_codes
                    WHERE phone_lookup_hash=:phone_hash AND purpose=:purpose
                    ORDER BY id DESC LIMIT 5
                    FOR UPDATE SKIP LOCKED
                    """
                ),
                {
                    "phone_hash": _phone_hash(normalized),
                    "purpose": purpose,
                },
            )
        ).mappings().all()
        now = _now()
        expected = _hash_value(code.strip())
        for row in rows:
            if (
                row["consumed_at"] is None
                and row["expires_at"] >= now
                and hmac.compare_digest(row["code_hash"], expected)
            ):
                updated = await connection.execute(
                    text(
                        """
                        UPDATE auth_verification_codes
                        SET consumed_at=:now
                        WHERE id=:id AND consumed_at IS NULL
                        """
                    ),
                    {"now": now, "id": row["id"]},
                )
                return updated.rowcount == 1
    return False


async def ensure_default_invite_code() -> None:
    from app.storage.auth import _seed_invite_codes

    codes = _seed_invite_codes()
    if not codes:
        return
    context = await _platform_context()
    async with auth_transaction() as connection:
        for code in codes:
            await connection.execute(
                text(
                    """
                    INSERT INTO auth_invite_codes (
                        tenant_id, code, max_uses, used_count
                    ) VALUES (:tenant, :code, :max_uses, 0)
                    ON CONFLICT (code) DO NOTHING
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "code": code,
                    "max_uses": settings.default_invite_max_uses,
                },
            )


async def consume_invite_code(code: str) -> bool:
    await ensure_default_invite_code()
    normalized = code.strip().upper()
    if not normalized:
        return False
    async with auth_transaction() as connection:
        updated = await connection.execute(
            text(
                """
                UPDATE auth_invite_codes
                SET used_count=used_count + 1
                WHERE code=:code AND disabled_at IS NULL
                  AND used_count < max_uses
                  AND (expires_at IS NULL OR expires_at >= CURRENT_TIMESTAMP)
                """
            ),
            {"code": normalized},
        )
        return updated.rowcount == 1


async def create_invite_code(
    code: str | None,
    max_uses: int,
    expires_at: str | None = None,
) -> dict:
    normalized = (code or secrets.token_urlsafe(8)).strip().upper()
    if len(normalized) < 3:
        raise ValueError("邀请码至少需要 3 个字符")
    if max_uses < 1:
        raise ValueError("最大使用次数至少为 1")
    context = await _platform_context()
    expiry = datetime.fromisoformat(expires_at) if expires_at else None
    try:
        async with auth_transaction() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO auth_invite_codes (
                        tenant_id, code, max_uses, expires_at
                    ) VALUES (:tenant, :code, :max_uses, :expires)
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "code": normalized,
                    "max_uses": max_uses,
                    "expires": expiry,
                },
            )
    except IntegrityError as exc:
        raise ValueError("邀请码已存在") from exc
    return await get_invite_code(normalized)


async def list_invite_codes() -> list[dict]:
    await ensure_default_invite_code()
    async with auth_transaction() as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT code, max_uses, used_count, expires_at,
                           disabled_at, created_at
                    FROM auth_invite_codes
                    ORDER BY created_at DESC LIMIT 100
                    """
                )
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def get_invite_code(code: str) -> dict:
    async with auth_transaction() as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT code, max_uses, used_count, expires_at,
                           disabled_at, created_at
                    FROM auth_invite_codes WHERE code=:code
                    """
                ),
                {"code": code},
            )
        ).mappings().one_or_none()
    if row is None:
        raise ValueError("邀请码不存在")
    return dict(row)


async def disable_invite_code(code: str) -> None:
    async with auth_transaction() as connection:
        await connection.execute(
            text(
                """
                UPDATE auth_invite_codes
                SET disabled_at=CURRENT_TIMESTAMP WHERE code=:code
                """
            ),
            {"code": code},
        )


async def create_user(
    phone: str,
    password: str,
    registered_via: str,
) -> dict:
    from app.storage.auth import hash_password, normalize_phone

    normalized = normalize_phone(phone)
    if normalized == "admin":
        raise ValueError("管理员账号不能通过注册创建")
    user_id = secrets.token_hex(16)
    context = user_principal(user_id)
    await reconcile_principal(context, display_name=normalized)
    now = _now()
    try:
        async with auth_transaction() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO auth_users (
                        id, tenant_id, principal_id, phone,
                        phone_lookup_hash, password_hash, registered_via,
                        created_at, last_login_at, updated_at
                    ) VALUES (
                        :id, :tenant, :principal, :phone,
                        :phone_hash, :password_hash, :registered_via,
                        :now, :now, :now
                    )
                    """
                ),
                {
                    "id": user_id,
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                    "phone": normalized,
                    "phone_hash": _phone_hash(normalized),
                    "password_hash": hash_password(password),
                    "registered_via": registered_via,
                    "now": now,
                },
            )
    except IntegrityError as exc:
        raise ValueError("该手机号已注册，请直接登录") from exc
    user = await get_user(user_id)
    if user is None:
        raise RuntimeError("用户创建失败")
    return user


async def reset_password(phone: str, password: str) -> dict:
    from app.storage.auth import (
        hash_password,
        is_admin_account,
        normalize_phone,
    )

    normalized = normalize_phone(phone)
    if is_admin_account(normalized):
        raise ValueError("管理员账号不支持验证码流程")
    async with auth_transaction() as connection:
        updated = await connection.execute(
            text(
                """
                UPDATE auth_users
                SET password_hash=:password_hash,
                    last_login_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE phone_lookup_hash=:phone_hash
                """
            ),
            {
                "password_hash": hash_password(password),
                "phone_hash": _phone_hash(normalized),
            },
        )
    if updated.rowcount != 1:
        raise ValueError("该手机号尚未注册，请先注册")
    user = await get_user_by_phone(normalized)
    if user is None:
        raise RuntimeError("密码重置后用户不可读")
    return user


async def authenticate_password(
    phone: str,
    password: str,
) -> dict | None:
    from app.storage.auth import (
        is_admin_account,
        normalize_phone,
        verify_password,
    )

    normalized = normalize_phone(phone)
    if is_admin_account(normalized):
        await ensure_admin_user()
    async with auth_transaction() as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT id, password_hash FROM auth_users
                    WHERE phone_lookup_hash=:phone_hash
                    """
                ),
                {"phone_hash": _phone_hash(normalized)},
            )
        ).mappings().one_or_none()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    await touch_login(row["id"])
    return await get_user(row["id"])


async def get_user_by_phone(phone: str) -> dict | None:
    from app.storage.auth import normalize_phone

    normalized = normalize_phone(phone)
    async with auth_transaction() as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT id, phone, registered_via, created_at, last_login_at
                    FROM auth_users WHERE phone_lookup_hash=:phone_hash
                    """
                ),
                {"phone_hash": _phone_hash(normalized)},
            )
        ).mappings().one_or_none()
    return _public(row) if row else None


async def touch_login(user_id: str) -> None:
    async with auth_transaction() as connection:
        await connection.execute(
            text(
                """
                UPDATE auth_users SET last_login_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP WHERE id=:id
                """
            ),
            {"id": user_id},
        )


async def get_user(user_id: str) -> dict | None:
    async with auth_transaction() as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT id, phone, registered_via, created_at, last_login_at
                    FROM auth_users WHERE id=:id
                    """
                ),
                {"id": user_id},
            )
        ).mappings().one_or_none()
    return _public(row) if row else None


async def delete_user(user_id: str) -> None:
    user = await get_user(user_id)
    if user and user["is_admin"]:
        raise ValueError("管理员账号不能注销")
    async with auth_transaction() as connection:
        await connection.execute(
            text("DELETE FROM auth_users WHERE id=:id"),
            {"id": user_id},
        )
