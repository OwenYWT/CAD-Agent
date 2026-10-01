"""Real PostgreSQL, auth tokens, tenant roles, and monitoring HTTP boundaries."""
import asyncio
import os
import secrets
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.db import close_database, tenant_transaction
from app.domain.identity import user_principal, TenantKind
from app.monitoring import app, reader
from app.repositories.identity import reconcile_principal, reconcile_authenticated_user
from app.services.llm_usage import UsageContext, usage_context, start_call, finish_call
from app.storage import auth


@pytest.mark.asyncio
async def test_separate_monitor_login_cannot_become_runtime_or_read_credentials(monitor, monkeypatch):
    from app.services.monitor_queries import close_monitor_database
    role = 'monitor_contract_' + uuid4().hex
    password = secrets.token_hex(24)
    admin_engine = create_async_engine(os.environ['CAD_MONITOR_TEST_DATABASE_URL'])
    async with admin_engine.begin() as conn:
        await conn.execute(text(f"CREATE ROLE {role} LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS PASSWORD '{password}'"))
        await conn.execute(text(f'GRANT cad_agent_monitor TO {role}'))
    url = make_url(os.environ['CAD_MONITOR_TEST_DATABASE_URL']).set(username=role, password=password)
    await close_monitor_database()
    monkeypatch.setattr(settings, 'monitor_database_url', url.render_as_string(hide_password=False))
    try:
        async with reader() as conn:
            assert await conn.scalar(text('SELECT session_user')) == role
            assert await conn.scalar(text('SELECT current_user')) == 'cad_agent_monitor'
            await conn.execute(text('SELECT id FROM llm_calls LIMIT 1'))
        for sql in ('SET LOCAL ROLE cad_agent_runtime', 'SELECT password_hash FROM auth_users',
                    'SELECT request_payload FROM workflow_runs', 'DELETE FROM llm_calls'):
            with pytest.raises(DBAPIError):
                async with reader() as conn:
                    await conn.execute(text(sql))
    finally:
        await close_monitor_database()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP ROLE {role}'))
        await admin_engine.dispose()

pytestmark = [pytest.mark.auth, pytest.mark.skipif(not os.getenv("CAD_MONITOR_TEST_DATABASE_URL"), reason="requires migrated isolated PostgreSQL")]


@pytest_asyncio.fixture
async def monitor(monkeypatch):
    await close_database()
    monkeypatch.setattr(settings, "database_url", os.environ["CAD_MONITOR_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "durable_control_plane_enabled", True)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "auth_token_secret", "monitor-tests-private-signing-key-not-a-deployment-secret")
    monkeypatch.setattr(settings, "admin_password", secrets.token_urlsafe(24))
    await auth.ensure_admin_user()
    admin = await auth.create_session_token("admin")
    phone = "19" + str(secrets.randbelow(10**9)).zfill(9)
    user = await auth.create_user(phone, secrets.token_urlsafe(24), "invite_code")
    principal = await reconcile_authenticated_user(user)
    user_token = await auth.create_session_token(user["id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://monitor") as client:
        yield client, {"Authorization": f"Bearer {admin}"}, user_token, principal, user
    await close_database()


@pytest.mark.asyncio
async def test_monitor_requires_real_platform_admin(monitor):
    client, admin, user_token, _, _ = monitor
    for path in ("accounts", "calls", "tasks"):
        assert (await client.get(f"/api/monitor/{path}")).status_code == 401
        assert (await client.get(f"/api/monitor/{path}", headers={"Authorization":f"Bearer {user_token}"})).status_code == 403
        response = await client.get(f"/api/monitor/{path}", headers=admin)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
    assert (await client.get("/api/monitor/accounts",headers=admin,params={"start":"2020-01-01T00:00:00Z"})).status_code == 422
    assert (await client.get("/api/monitor/calls",headers=admin,params={"offset":-1})).status_code == 422


@pytest.mark.asyncio
async def test_actual_usage_nullable_failures_and_cross_workspace_account_grouping(monitor):
    client, admin, _, principal, user = monitor
    shared = user_principal(user["id"],tenant_id=uuid4(),tenant_kind=TenantKind.ORGANIZATION)
    await reconcile_principal(shared, display_name="共享工作区别名")
    for owner, error, provenance in [
        (principal,None,{"usage":{"prompt_tokens":11,"completion_tokens":23,"total_tokens":34,"completion_tokens_details":{"reasoning_tokens":7}},"finish_reason":"stop"}),
        (shared,ValueError("private response should never be persisted"),None),
    ]:
        token=usage_context.set(UsageContext(owner.tenant_id,owner.principal_id))
        try:
            handle=await start_call(provider="test-fixture",model="fixture",request_hash="f"*64,attempt=1)
            assert handle is not None
            await finish_call(handle,duration_ms=25,provenance=provenance,error=error)
        finally: usage_context.reset(token)
    response=await client.get("/api/monitor/accounts",headers=admin)
    assert response.status_code == 200,response.text
    row=next(x for x in response.json()["accounts"] if x["account"]==principal.external_subject)
    assert row["calls"]==2 and row["total_tokens"]==34 and row["unknown_usage_calls"]==1
    assert row["workspaces"]==2 and row["call_failures"]==1 and row["reasoning_tokens"]==7
    assert row["name"] == user["phone"]
    records=(await client.get("/api/monitor/calls",headers=admin,params={"account":principal.external_subject})).json()["items"]
    assert len(records)==2
    assert "private response" not in str(records)
    assert next(x for x in records if x["status"]=="failed")["total_tokens"] is None
    # Runtime retains tenant isolation despite the administrator reader's policy.
    async with tenant_transaction(principal.tenant_id,principal.principal_id) as conn:
        assert await conn.scalar(text("SELECT count(*) FROM llm_calls WHERE principal_id=:id"),{"id":shared.principal_id}) == 0


@pytest.mark.asyncio
async def test_reader_cannot_write_or_read_prompts_or_credentials(monitor):
    for sql in ("SELECT password_hash FROM auth_users", "SELECT request_payload FROM workflow_runs", "DELETE FROM llm_calls"):
        with pytest.raises(DBAPIError):
            async with reader() as conn: await conn.execute(text(sql))


@pytest.mark.asyncio
async def test_cancel_and_unfinished_are_not_success(monitor):
    client,admin,_,principal,_=monitor
    token=usage_context.set(UsageContext(principal.tenant_id,principal.principal_id))
    try:
        first=await start_call(provider="test-fixture",model="fixture",request_hash="f"*64,attempt=1)
        await finish_call(first,duration_ms=1,error=asyncio.CancelledError())
        await start_call(provider="test-fixture",model="fixture",request_hash="f"*64,attempt=2)
    finally:usage_context.reset(token)
    records=(await client.get("/api/monitor/calls",headers=admin,params={"account":principal.external_subject})).json()["items"]
    assert {x["status"] for x in records}=={"cancelled","started"}
    assert all(x["total_tokens"] is None for x in records)


@pytest.mark.asyncio
async def test_usage_context_does_not_cross_concurrent_accounts(monitor):
    client,admin,_,principal,_=monitor
    other = user_principal(str(uuid4()))
    await reconcile_principal(other)
    async def run(owner,attempt):
        token=usage_context.set(UsageContext(owner.tenant_id,owner.principal_id))
        try:
            await asyncio.sleep(0)
            handle=await start_call(provider="test-fixture",model="fixture",request_hash="f"*64,attempt=attempt)
            assert handle is not None
            await finish_call(handle,duration_ms=1,provenance={"finish_reason":"stop","usage":{"total_tokens":attempt}})
        finally:usage_context.reset(token)
    await asyncio.gather(run(principal,1),run(other,2))
    for owner,expected in [(principal,1),(other,2)]:
        rows=(await client.get("/api/monitor/calls",headers=admin,params={"account":owner.external_subject})).json()["items"]
        assert len(rows)==1 and rows[0]["total_tokens"]==expected
