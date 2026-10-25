"""Customer billing authorization, persistence and quota with real PostgreSQL."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import secrets
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from app.api.billing import router as billing_router
from app.api.login import router as login_router
from app.config import settings
from app.contracts.provider_budget import ProviderBudgetError
from app.db import close_database, tenant_transaction
from app.domain.identity import user_principal
from app.services import customer_billing as billing, model_jobs
from app.services.provider_budgets import PgProviderBudget, admit, freeze_policy
from app.storage import auth
from tests.postgres.test_model_jobs import request_job  # noqa: F401


DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.auth,
    pytest.mark.skipif(not DATABASE_URL, reason="isolated PostgreSQL required"),
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "database_url", DATABASE_URL)
    monkeypatch.setattr(settings, "durable_control_plane_enabled", True)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "api_keys", [])
    monkeypatch.setattr(settings, "auth_token_secret", "customer-billing-test-" + "s" * 64)
    monkeypatch.setattr(settings, "admin_password", "billing-test-administrator-password")
    asyncio.run(close_database())
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")

    @asynccontextmanager
    async def lifespan(app):
        yield
        await close_database()

    app = FastAPI(lifespan=lifespan)
    app.include_router(login_router)
    app.include_router(billing_router)
    with TestClient(app) as instance:
        yield instance


def login(client, *, admin=False):
    phone = "admin" if admin else "20261021" + str(secrets.randbelow(10**10)).zfill(10)
    password = settings.admin_password if admin else secrets.token_urlsafe(24)
    if not admin:
        client.portal.call(auth.create_user, phone, password, "integration_test")
    response = client.post("/api/auth/login/password", json={"phone": phone, "password": password})
    assert response.status_code == 200
    body = response.json()
    return user_principal(body["user"]["id"]), {"Authorization": "Bearer " + body["token"]}


def test_customer_accounts_are_private_and_cannot_self_upgrade_or_credit(client):
    first, first_headers = login(client)
    second, second_headers = login(client)
    response = client.get("/api/billing/me", headers=first_headers)
    assert response.status_code == 200
    summary = response.json()
    assert summary["tenant_id"] == str(first.tenant_id)
    assert summary["plan"]["code"] == "basic"
    assert summary["wallet"]["balance_minor"] == 0
    assert summary["online_payments_enabled"] is False
    assert client.get("/api/billing/me").status_code == 401
    assert client.get("/api/billing/me", headers={
        **second_headers, "X-Workspace-Tenant": str(first.tenant_id),
    }).status_code == 403
    assert client.get("/api/billing/admin/plans", headers=first_headers).status_code == 403
    body = {"amount_minor": 500, "reference": "test-grant", "reason": "人工测试调整",
            "idempotency_key": uuid4().hex}
    assert client.post(f"/api/billing/admin/tenants/{first.tenant_id}/adjustments",
                       headers=second_headers, json=body).status_code == 403
    assert client.get("/api/billing/me", headers=second_headers).json()["wallet"]["balance_minor"] == 0

    async def sql_cannot_self_credit():
        async with tenant_transaction(first.tenant_id, first.principal_id) as connection:
            assert await connection.scalar(text("SELECT count(*) FROM tenant_wallet_accounts WHERE tenant_id=:id"),
                                           {"id": second.tenant_id}) == 0
        with pytest.raises(Exception, match="permission denied"):
            async with tenant_transaction(first.tenant_id, first.principal_id) as connection:
                await connection.execute(text("UPDATE tenant_wallet_accounts SET balance_minor=500"))

    client.portal.call(sql_cannot_self_credit)


def test_manual_adjustments_are_idempotent_audited_and_never_report_payment_success(client):
    principal, headers = login(client)
    _, admin_headers = login(client, admin=True)
    path = f"/api/billing/admin/tenants/{principal.tenant_id}/adjustments"
    body = {"amount_minor": 500, "reference": "manual-grant-" + uuid4().hex,
            "reason": "管理员人工调账；不是支付回调", "idempotency_key": uuid4().hex}
    first = client.post(path, headers=admin_headers, json=body)
    assert first.status_code == 200
    assert first.json()["kind"] == "admin_adjustment"
    assert client.post(path, headers=admin_headers, json=body).json() == first.json()
    assert client.post(path, headers=admin_headers, json={**body, "amount_minor": 501}).status_code == 409
    assert client.post(path, headers=admin_headers, json={
        **body, "idempotency_key": uuid4().hex, "amount_minor": -501,
    }).status_code == 409
    assert client.post(path, headers=admin_headers, json={
        **body, "idempotency_key": uuid4().hex, "amount_minor": 0.5,
    }).status_code == 422
    assert client.get("/api/billing/me", headers=headers).json()["wallet"]["balance_minor"] == 500
    assert len(client.get("/api/billing/entries", headers=headers).json()) == 1

    # Exercise concurrent real transactions. Immutability is checked separately
    # through the runtime role, whose SQL privileges cannot rewrite entries.
    async def retries():
        concurrent_body = {**body, "idempotency_key": uuid4().hex}
        async def adjust():
            async with tenant_transaction(principal.tenant_id, role="billing_admin") as connection:
                return await billing.adjust_wallet(connection, tenant_id=principal.tenant_id,
                    actor_subject="user:admin", **concurrent_body)
        assert len({str(row["id"]) for row in await asyncio.gather(*(adjust() for _ in range(8)))}) == 1
        with pytest.raises(Exception, match="permission denied"):
            async with tenant_transaction(principal.tenant_id, role="billing_admin") as connection:
                await connection.execute(text("UPDATE tenant_wallet_entries SET amount_minor=999"))
    client.portal.call(retries)
    assert client.get("/api/billing/me", headers=headers).json()["wallet"]["balance_minor"] == 1000
    assert len(client.get("/api/billing/entries", headers=headers).json()) == 2


def test_three_tiers_versioned_assignment_and_no_unconfigured_paid_entitlement(client):
    principal, headers = login(client)
    _, admin_headers = login(client, admin=True)
    catalog = client.get("/api/billing/admin/plans", headers=admin_headers).json()
    assert {row["code"] for row in catalog} == {"basic", "pro", "team"}
    draft = next(row for row in catalog if row["code"] == "pro" and row["version"] == 1)
    path = f"/api/billing/admin/tenants/{principal.tenant_id}/subscription"
    assignment = {"plan_version_id": draft["id"], "expected_revision": 0,
                  "idempotency_key": uuid4().hex}
    assert client.put(path, headers=admin_headers, json=assignment).status_code == 409
    version = max(row["version"] for row in catalog if row["code"] == "pro") + 1
    values = {"code": "pro", "version": version, "name": "专业版",
              "published": True, "monthly_price_minor": 19900, "monthly_provider_requests": 50}
    assert client.post("/api/billing/admin/plans", headers=admin_headers, json={
        **values, "monthly_price_minor": None,
    }).status_code == 422
    created = client.post("/api/billing/admin/plans", headers=admin_headers, json=values)
    assert created.status_code == 201
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    assignment.update(plan_version_id=created.json()["id"], expires_at=expires)
    response = client.put(path, headers=admin_headers, json=assignment)
    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert client.put(path, headers=admin_headers, json=assignment).json() == response.json()
    assert client.put(path, headers=admin_headers, json={
        **assignment, "idempotency_key": uuid4().hex,
    }).status_code == 409
    summary = client.get("/api/billing/me", headers=headers).json()
    assert summary["plan"]["version"] == version and summary["plan"]["monthly_provider_requests"] == 50
    assert summary["wallet"]["balance_minor"] == 0


def test_invited_project_member_cannot_read_or_change_workspace_billing(client):
    from app.repositories.projects import create_project
    from app.repositories.revisions import create_initial_branch
    from app.services.document_sharing import create_review_invite, accept_review_invite
    from app.services.cloud_documents import document_snapshot

    owner, owner_headers = login(client)
    guest, guest_headers = login(client)

    async def invite():
        project = uuid4()
        async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
            await create_project(connection, project_id=project, tenant_id=owner.tenant_id,
                creator_principal_id=owner.principal_id, name="Billing isolation", slug=project.hex)
            branch = await create_initial_branch(connection, tenant_id=owner.tenant_id,
                project_id=project, created_by_principal_id=owner.principal_id,
                branch_name="main", initial_manifest={})
        invitation = await create_review_invite(owner, branch.branch_id, role="editor")
        await accept_review_invite(guest, branch.branch_id, owner.tenant_id, invitation["token"])
        member = user_principal(guest.external_subject.removeprefix("user:"), tenant_id=owner.tenant_id)
        assert (await document_snapshot(member, branch.branch_id))["can_edit"]

    client.portal.call(invite)
    workspace_headers = {**guest_headers, "X-Workspace-Tenant": str(owner.tenant_id)}
    assert client.get("/api/billing/plans", headers=workspace_headers).status_code == 200
    for path in ("/api/billing/me", "/api/billing/entries"):
        assert client.get(path, headers=workspace_headers).status_code == 403
        assert client.get(path, headers=owner_headers).status_code == 200
    assert client.get("/api/billing/me", headers=guest_headers).json()["tenant_id"] == str(guest.tenant_id)


def test_wallet_concurrent_debits_and_failed_transaction_preserve_ledger(client):
    principal, headers = login(client)
    _, admin_headers = login(client, admin=True)
    path = f"/api/billing/admin/tenants/{principal.tenant_id}/adjustments"
    body = {"amount_minor": 500, "reference": "grant", "reason": "测试并发余额",
            "idempotency_key": uuid4().hex}
    assert client.post(path, headers=admin_headers, json=body).status_code == 200

    async def debit_and_abort():
        async def debit():
            async with tenant_transaction(principal.tenant_id, role="billing_admin") as connection:
                return await billing.adjust_wallet(connection, tenant_id=principal.tenant_id,
                    actor_subject="user:admin", **{**body, "amount_minor": -400, "idempotency_key": uuid4().hex})
        results = await asyncio.gather(debit(), debit(), return_exceptions=True)
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, billing.BillingError) and result.code == "wallet_balance_invalid"
                   for result in results) == 1
        # A real rollback after both writes must leave neither credit nor entry.
        with pytest.raises(RuntimeError, match="abort before commit"):
            async with tenant_transaction(principal.tenant_id, role="billing_admin") as connection:
                await billing.adjust_wallet(connection, tenant_id=principal.tenant_id,
                    actor_subject="user:admin", **{**body, "idempotency_key": uuid4().hex})
                raise RuntimeError("abort before commit")

    client.portal.call(debit_and_abort)
    assert client.get("/api/billing/me", headers=headers).json()["wallet"]["balance_minor"] == 100
    entries = client.get("/api/billing/entries", headers=headers).json()
    assert len(entries) == 2 and sum(row["amount_minor"] for row in entries) == 100


@pytest.mark.asyncio
async def test_subscription_quota_is_checked_in_real_provider_reservation(request_job):
    payload = request_job["payload"]
    tenant, principal = UUID(payload["tenant_id"]), UUID(payload["principal_id"])
    async with tenant_transaction(tenant, principal) as connection:
        await admit(connection, tenant_id=tenant, workflow_id=UUID(payload["workflow_run_id"]),
                    policy=freeze_policy(mode="requests", request_limit=20, month_request_limit=100))
    async with tenant_transaction(tenant, role="billing_admin") as connection:
        version = await connection.scalar(text("SELECT max(version)+1 FROM billing_plan_versions WHERE code='pro'"))
        plan = await billing.create_plan_version(connection, actor_subject="user:admin", values={
            "code": "pro", "version": version, "name": "Quota test", "published": True,
            "monthly_price_minor": 100, "currency": "CNY", "monthly_provider_requests": 1,
        })
        await billing.assign_subscription(connection, tenant_id=tenant, actor_subject="user:admin",
            idempotency_key=uuid4().hex, plan_version_id=UUID(plan["id"]), expected_revision=0,
            expires_at=None, status="active")
    await model_jobs.submit(request_job)
    job = await model_jobs.claim()
    assert str(job["id"]) == request_job["job_id"]
    budget = PgProviderBudget(job)
    await budget.prepare_request({"model": "kimi-k2.7-code", "messages": []}, provider="moonshot")

    async def reserve(logical):
        return await budget.reserve_attempt(logical_call_id=logical, attempt=1,
            request_digest="quota", provider="moonshot", model="kimi-k2.7-code")

    results = await asyncio.gather(*(reserve(str(index)) for index in range(4)), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert all(isinstance(result, dict) or (
        isinstance(result, ProviderBudgetError) and result.code == "tenant_quota_exhausted"
    ) for result in results)
    row = next(result for result in results if isinstance(result, dict))
    assert (await reserve(row["logical_call_id"]))["id"] == row["id"]
    async with tenant_transaction(tenant, principal) as connection:
        assert await billing.month_usage(connection, tenant) == 1
        assert await connection.scalar(text("SELECT count(*) FROM provider_budget_reservations")) == 1
        assert await connection.scalar(text("SELECT max(requests_used) FROM provider_budget_accounts")) == 1

    # A plan change grants only the new total, never a fresh usage counter.
    async with tenant_transaction(tenant, role="billing_admin") as connection:
        upgraded = await billing.create_plan_version(connection, actor_subject="user:admin", values={
            "code": "pro", "version": version + 1, "name": "Quota upgrade", "published": True,
            "monthly_price_minor": 200, "currency": "CNY", "monthly_provider_requests": 2,
        })
        await billing.assign_subscription(connection, tenant_id=tenant, actor_subject="user:admin",
            idempotency_key=uuid4().hex, plan_version_id=UUID(upgraded["id"]), expected_revision=1,
            expires_at=None, status="active")
    await reserve("after-upgrade")
    with pytest.raises(ProviderBudgetError) as exhausted:
        await reserve("over-upgrade")
    assert exhausted.value.code == "tenant_quota_exhausted"
    async with tenant_transaction(tenant, principal) as connection:
        assert await billing.month_usage(connection, tenant) == 2
        assert await connection.scalar(text("SELECT max(request_limit) FROM provider_budget_accounts")) == 100

    # Pausing/expiry blocks new requests, but previously persisted responses,
    # account reads and reservation identity are still available for recovery.
    async with tenant_transaction(tenant, role="billing_admin") as connection:
        await billing.assign_subscription(connection, tenant_id=tenant, actor_subject="user:admin",
            idempotency_key=uuid4().hex, plan_version_id=UUID(upgraded["id"]), expected_revision=2,
            expires_at=None, status="suspended")
    with pytest.raises(ProviderBudgetError) as suspended:
        await reserve("after-suspension")
    assert suspended.value.code == "subscription_inactive"
    assert (await reserve(row["logical_call_id"]))["id"] == row["id"]
    async with tenant_transaction(tenant, role="billing_admin") as connection:
        # Persist an already expired historical subscription without a clock mock.
        await connection.execute(text("UPDATE tenant_subscriptions SET status='active',expires_at=now()-interval '1 second'"))
    with pytest.raises(ProviderBudgetError) as expired:
        await reserve("after-expiry")
    assert expired.value.code == "subscription_inactive"
    async with tenant_transaction(tenant, principal) as connection:
        summary = await billing.account_summary(connection, tenant, principal)
        assert summary["subscription"]["status"] == "expired"
        assert summary["usage"]["provider_requests"] == 2
    from app.repositories.identity import reconcile_principal
    other = user_principal("quota-other-" + uuid4().hex)
    await reconcile_principal(other)
    async with tenant_transaction(other.tenant_id, other.principal_id) as connection:
        await billing.check_provider_request(connection, other.tenant_id)
        assert await billing.month_usage(connection, other.tenant_id) == 0
