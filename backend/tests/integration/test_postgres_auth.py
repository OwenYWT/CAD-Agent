"""Real authentication flow through PostgreSQL, with SQLite disabled."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import settings
from app.db import auth_transaction, close_database
from app.storage import auth


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="CAD_AGENT_TEST_DATABASE_URL is required",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    if not TEST_DATABASE_URL:
        yield
        return
    original = {
        "database_url": settings.database_url,
        "durable": settings.durable_control_plane_enabled,
        "secret": settings.auth_token_secret,
        "history": settings.history_db_path,
    }
    settings.database_url = TEST_DATABASE_URL
    settings.durable_control_plane_enabled = True
    settings.auth_token_secret = "test-only-" + "s" * 64
    settings.history_db_path = "/path-that-must-not-be-created/auth-history.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield
    settings.database_url = original["database_url"]
    settings.durable_control_plane_enabled = original["durable"]
    settings.auth_token_secret = original["secret"]
    settings.history_db_path = original["history"]


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def database_engine_lifecycle(migrated_database):
    yield
    await close_database()


@pytest.mark.asyncio(loop_scope="module")
async def test_register_login_session_codes_and_atomic_invite_consumption():
    suffix = uuid4().hex
    phone = f"+861{int(suffix[:10], 16) % 10_000_000_000:010d}"
    invite = f"PG-{suffix}"
    stored_invite = invite
    user = None
    try:
        created_invite = await auth.create_invite_code(invite, 1)
        stored_invite = created_invite["code"]
        assert created_invite["used_count"] == 0
        consumed = await asyncio.gather(
            auth.consume_invite_code(invite),
            auth.consume_invite_code(invite),
        )
        assert sorted(consumed) == [False, True]

        user = await auth.create_user(phone, "strong-password-123", "invite_code")
        assert user["phone"] == phone
        assert not user["is_admin"]
        assert await auth.authenticate_password(phone, "wrong-password") is None
        logged_in = await auth.authenticate_password(
            phone,
            "strong-password-123",
        )
        assert logged_in and logged_in["id"] == user["id"]

        token = await auth.create_session_token(user["id"])
        assert await auth.verify_session_token(token) == user["id"]
        refreshed = await auth.refresh_session_token(token)
        assert refreshed and refreshed != token
        assert await auth.verify_session_token(token) is None
        assert await auth.verify_session_token(refreshed) == user["id"]
        await auth.revoke_session_token(refreshed)
        assert await auth.verify_session_token(refreshed) is None

        code = await auth.issue_verification_code(phone, "reset_password")
        assert await auth.consume_verification_code(
            phone,
            "reset_password",
            code,
        )
        assert not await auth.consume_verification_code(
            phone,
            "reset_password",
            code,
        )
    finally:
        if user is not None:
            await auth.delete_user(user["id"])
        async with auth_transaction() as connection:
            await connection.execute(
                text("DELETE FROM auth_invite_codes WHERE code=:code"),
                {"code": stored_invite},
            )
