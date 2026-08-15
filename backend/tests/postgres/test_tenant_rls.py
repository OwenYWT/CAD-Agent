"""Real PostgreSQL tests for M1 tenant ownership and row isolation.

Set CAD_AGENT_TEST_DATABASE_URL to an empty disposable PostgreSQL database.
These tests run Alembic against that database and use the real runtime/worker
roles created by the migration; no in-memory database or repository fake is
accepted as evidence.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text

from app.api.auth import get_current_user, get_optional_user, verify_api_key
from app.api.login import _auth_response
from app.config import settings
from app.db import close_database, get_database_engine, tenant_transaction
from app.domain.identity import (
    PrincipalKind,
    TenantKind,
    api_key_principal,
    local_anonymous_principal,
    quarantine_principal,
    user_principal,
)
from app.domain.projects import Permission, ProjectRole, role_allows
from app.repositories.audit import append_audit_record
from app.repositories.identity import ensure_principal
from app.repositories.projects import (
    add_project_member,
    create_project,
    principal_has_permission,
)
from app.repositories.usage import record_usage
from app.storage import auth as legacy_auth


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
ORIGINAL_DATABASE_URL = settings.database_url

pytestmark = [
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="CAD_AGENT_TEST_DATABASE_URL is required for real PostgreSQL tests",
    ),
]


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    if not TEST_DATABASE_URL:
        yield
        return

    settings.database_url = TEST_DATABASE_URL
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture(
    scope="module",
    autouse=True,
    loop_scope="module",
)
async def database_engine_lifecycle(migrated_database):
    settings.database_url = TEST_DATABASE_URL
    yield
    await close_database()
    settings.database_url = ORIGINAL_DATABASE_URL


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean_control_plane():
    if not TEST_DATABASE_URL:
        yield
        return

    async with get_database_engine().begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE usage_meter_entries, audit_records, "
                "project_memberships, projects, tenant_memberships, "
                "principals, tenants CASCADE"
            )
        )
    yield


def test_identity_mappings_are_stable_and_never_retain_raw_api_keys():
    user = user_principal("legacy-user-42")
    api_key = api_key_principal("do-not-store-this-key")
    local = local_anonymous_principal()
    quarantine = quarantine_principal("ownerless-history-row")

    assert user == user_principal("legacy-user-42")
    assert user.kind is PrincipalKind.USER
    assert user.tenant_id != api_key.tenant_id
    assert local.tenant_id != quarantine.tenant_id
    assert api_key.api_key_fingerprint
    assert "do-not-store-this-key" not in repr(api_key)
    for value in (
        user.tenant_id,
        user.principal_id,
        api_key.tenant_id,
        api_key.principal_id,
        local.tenant_id,
        quarantine.tenant_id,
    ):
        assert isinstance(value, UUID)


def test_one_authenticated_user_can_have_distinct_principals_in_multiple_tenants():
    first_tenant = uuid4()
    second_tenant = uuid4()

    first = user_principal(
        "shared-user",
        tenant_id=first_tenant,
        tenant_kind=TenantKind.ORGANIZATION,
    )
    second = user_principal(
        "shared-user",
        tenant_id=second_tenant,
        tenant_kind=TenantKind.ORGANIZATION,
    )

    assert first.external_subject == second.external_subject
    assert first.principal_id != second.principal_id
    assert first.tenant_id != second.tenant_id


@pytest.mark.parametrize(
    ("role", "permission", "allowed"),
    [
        (ProjectRole.OWNER, Permission.DELETE_PROJECT, True),
        (ProjectRole.ADMIN, Permission.MANAGE_MEMBERS, True),
        (ProjectRole.EDITOR, Permission.MODIFY_DESIGN, True),
        (ProjectRole.EDITOR, Permission.MANAGE_MEMBERS, False),
        (ProjectRole.VIEWER, Permission.VIEW_PROJECT, True),
        (ProjectRole.VIEWER, Permission.MODIFY_DESIGN, False),
    ],
)
def test_project_policy_decisions_are_explicit(role, permission, allowed):
    assert role_allows(role, permission) is allowed


@pytest.mark.asyncio(loop_scope="module")
async def test_personal_tenant_project_membership_audit_and_usage_are_real():
    owner = user_principal("user-one")
    editor = user_principal("user-two", tenant_id=owner.tenant_id)
    project_id = uuid4()

    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        await ensure_principal(connection, owner, display_name="User One")
        await ensure_principal(connection, editor, display_name="User Two")
        await create_project(
            connection,
            project_id=project_id,
            tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id,
            name="Gearbox",
            slug="gearbox",
        )
        await add_project_member(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=editor.principal_id,
            role=ProjectRole.EDITOR,
        )
        audit_id = await append_audit_record(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            actor_principal_id=owner.principal_id,
            action="project.created",
            target_type="project",
            target_id=str(project_id),
            payload={"source": "postgres-test"},
        )
        usage_id = await record_usage(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            metric="mcad_execution_seconds",
            quantity=7,
            unit="second",
            billing_dimension="compute.cpu_seconds",
            idempotency_key="usage-project-one",
        )
        replayed_usage_id = await record_usage(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            metric="mcad_execution_seconds",
            quantity=7,
            unit="second",
            billing_dimension="compute.cpu_seconds",
            idempotency_key="usage-project-one",
        )
        with pytest.raises(ValueError, match="different payload"):
            await record_usage(
                connection,
                tenant_id=owner.tenant_id,
                project_id=project_id,
                principal_id=owner.principal_id,
                metric="mcad_execution_seconds",
                quantity=8,
                unit="second",
                billing_dimension="compute.cpu_seconds",
                idempotency_key="usage-project-one",
            )

        assert await principal_has_permission(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=editor.principal_id,
            permission=Permission.MODIFY_DESIGN,
        )
        assert not await principal_has_permission(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=editor.principal_id,
            permission=Permission.MANAGE_MEMBERS,
        )
        assert replayed_usage_id == usage_id
        stored = (
            await connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM projects WHERE id=:project_id) AS projects, "
                    "(SELECT count(*) FROM audit_records WHERE id=:audit_id) AS audits, "
                    "(SELECT count(*) FROM usage_meter_entries WHERE id=:usage_id) AS usage"
                ),
                {
                    "project_id": project_id,
                    "audit_id": audit_id,
                    "usage_id": usage_id,
                },
            )
        ).mappings().one()

    assert stored == {"projects": 1, "audits": 1, "usage": 1}


@pytest.mark.asyncio(loop_scope="module")
async def test_api_key_service_principal_stores_only_sha256_fingerprint():
    raw_key = "commercial-api-key-that-must-not-enter-postgres"
    principal = api_key_principal(raw_key)

    async with tenant_transaction(
        principal.tenant_id,
        principal.principal_id,
    ) as connection:
        await ensure_principal(connection, principal, display_name="Build service")
        row = (
            await connection.execute(
                text(
                    "SELECT external_subject, api_key_fingerprint "
                    "FROM principals WHERE id=:principal_id"
                ),
                {"principal_id": principal.principal_id},
            )
        ).mappings().one()

    assert row["api_key_fingerprint"] == principal.api_key_fingerprint
    assert raw_key not in str(row)


@pytest.mark.asyncio(loop_scope="module")
async def test_real_auth_dependency_binds_api_key_to_persisted_service_tenant(
    monkeypatch,
):
    raw_key = "request-path-commercial-key"
    monkeypatch.setattr(settings, "durable_control_plane_enabled", True)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "api_keys", [raw_key])
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/parts/example",
            "headers": [],
        }
    )

    credential = await verify_api_key(
        request,
        bearer=None,
        x_api_key=raw_key,
    )
    context = request.state.principal_context

    assert credential == raw_key  # legacy route contract remains unchanged
    assert context == api_key_principal(raw_key)
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        stored = (
            await connection.execute(
                text(
                    "SELECT kind, api_key_fingerprint FROM principals "
                    "WHERE id=:principal_id"
                ),
                {"principal_id": context.principal_id},
            )
        ).mappings().one()
    assert stored["kind"] == "service"
    assert stored["api_key_fingerprint"] == context.api_key_fingerprint


@pytest.mark.asyncio(loop_scope="module")
async def test_real_login_session_binds_user_to_personal_tenant(
    monkeypatch,
    tmp_path,
):
    original_history_path = settings.history_db_path
    await legacy_auth.close_db()
    monkeypatch.setattr(settings, "history_db_path", str(tmp_path / "history.db"))
    monkeypatch.setattr(settings, "durable_control_plane_enabled", True)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "api_keys", [])
    user = await legacy_auth.create_user(
        "13800000991",
        "strong-password",
        "postgres-test",
    )
    auth_response = await _auth_response(user)
    token = auth_response["token"]
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/projects",
            "headers": [],
        }
    )

    credential = await verify_api_key(
        request,
        bearer=HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials=token,
        ),
        x_api_key=None,
    )
    context = request.state.principal_context

    assert credential == f"user:{user['id']}"
    assert context == user_principal(user["id"])
    dependency_credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=token,
    )
    current_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/auth/me",
            "headers": [],
        }
    )
    optional_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/history/sessions",
            "headers": [],
        }
    )
    assert await get_current_user(current_request, dependency_credentials) == user
    assert await get_optional_user(optional_request, dependency_credentials) == user
    assert current_request.state.principal_context == context
    assert optional_request.state.principal_context == context
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM principals WHERE id=:id"),
                {"id": context.principal_id},
            )
        ) == 1

    await legacy_auth.close_db()
    settings.history_db_path = original_history_path


@pytest.mark.asyncio(loop_scope="module")
async def test_local_and_quarantine_mappings_persist_with_explicit_status():
    local = local_anonymous_principal()
    quarantine = quarantine_principal("ownerless-snapshot")

    for context in (local, quarantine):
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            await ensure_principal(connection, context)
            stored = (
                await connection.execute(
                    text(
                        "SELECT t.kind AS tenant_kind, t.status AS tenant_status, "
                        "p.kind AS principal_kind, p.status AS principal_status "
                        "FROM tenants t JOIN principals p ON p.tenant_id=t.id "
                        "WHERE p.id=:principal_id"
                    ),
                    {"principal_id": context.principal_id},
                )
            ).mappings().one()
        assert stored["tenant_kind"] == context.tenant_kind.value
        assert stored["principal_kind"] == context.kind.value
        assert stored["tenant_status"] == (
            "quarantined" if context.quarantined else "active"
        )
        assert stored["principal_status"] == (
            "quarantined" if context.quarantined else "active"
        )


@pytest.mark.asyncio(loop_scope="module")
async def test_runtime_and_worker_roles_cannot_bypass_rls():
    async with get_database_engine().connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT rolname, rolsuper, rolbypassrls "
                    "FROM pg_roles "
                    "WHERE rolname IN ('cad_agent_runtime', 'cad_agent_worker') "
                    "ORDER BY rolname"
                )
            )
        ).mappings().all()
        table_owner = await connection.scalar(
            text(
                "SELECT tableowner FROM pg_tables "
                "WHERE schemaname='public' AND tablename='projects'"
            )
        )

    assert [row["rolname"] for row in rows] == [
        "cad_agent_runtime",
        "cad_agent_worker",
    ]
    assert all(not row["rolsuper"] and not row["rolbypassrls"] for row in rows)
    assert table_owner not in {"cad_agent_runtime", "cad_agent_worker"}


@pytest.mark.asyncio(loop_scope="module")
async def test_every_control_plane_table_forces_rls_and_usage_is_provider_neutral():
    expected_tables = {
        "tenants",
        "principals",
        "tenant_memberships",
        "projects",
        "project_memberships",
        "audit_records",
        "usage_meter_entries",
    }
    async with get_database_engine().connect() as connection:
        rls_rows = (
            await connection.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class "
                    "WHERE relname = ANY(:table_names)"
                ),
                {"table_names": list(expected_tables)},
            )
        ).mappings().all()
        usage_columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' "
                        "AND table_name='usage_meter_entries'"
                    )
                )
            ).scalars()
        )

    assert {row["relname"] for row in rls_rows} == expected_tables
    assert all(row["relrowsecurity"] for row in rls_rows)
    assert all(row["relforcerowsecurity"] for row in rls_rows)
    assert not any(
        provider in column.lower()
        for column in usage_columns
        for provider in ("stripe", "paypal", "wechat", "alipay")
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_cross_tenant_ids_fail_and_pooled_transactions_do_not_leak_context():
    first = user_principal("tenant-one-user")
    second = user_principal("tenant-two-user")
    first_project = uuid4()
    second_project = uuid4()

    for principal, project_id, slug in (
        (first, first_project, "first"),
        (second, second_project, "second"),
    ):
        async with tenant_transaction(
            principal.tenant_id,
            principal.principal_id,
        ) as connection:
            await ensure_principal(connection, principal)
            await create_project(
                connection,
                project_id=project_id,
                tenant_id=principal.tenant_id,
                creator_principal_id=principal.principal_id,
                name=slug,
                slug=slug,
            )

    async with tenant_transaction(first.tenant_id, first.principal_id) as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM projects WHERE id=:project_id"),
                {"project_id": second_project},
            )
        ) == 0
        with pytest.raises(Exception, match="row-level security|foreign key"):
            await create_project(
                connection,
                project_id=uuid4(),
                tenant_id=second.tenant_id,
                creator_principal_id=second.principal_id,
                name="cross tenant",
                slug="cross-tenant",
            )

    async def visible_ids(principal):
        async with tenant_transaction(
            principal.tenant_id,
            principal.principal_id,
        ) as connection:
            await asyncio.sleep(0)
            return set(
                (
                    await connection.execute(
                        text("SELECT id FROM projects ORDER BY id")
                    )
                ).scalars()
            )

    results = await asyncio.gather(
        *(visible_ids(first if index % 2 == 0 else second) for index in range(20))
    )
    assert all(
        result == ({first_project} if index % 2 == 0 else {second_project})
        for index, result in enumerate(results)
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_audit_records_are_append_only_even_inside_the_same_tenant():
    owner = user_principal("audit-owner")

    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        await ensure_principal(connection, owner)
        audit_id = await append_audit_record(
            connection,
            tenant_id=owner.tenant_id,
            project_id=None,
            actor_principal_id=owner.principal_id,
            action="tenant.created",
            target_type="tenant",
            target_id=str(owner.tenant_id),
            payload={
                "password": "must-never-be-stored",
                "nested": {"authorization": "Bearer must-not-survive"},
                "safe": "retained",
            },
        )
        stored_payload = await connection.scalar(
            text("SELECT payload FROM audit_records WHERE id=:id"),
            {"id": audit_id},
        )

    assert stored_payload == {
        "password": "[REDACTED]",
        "nested": {"authorization": "[REDACTED]"},
        "safe": "retained",
    }
    assert "must-never-be-stored" not in str(stored_payload)
    assert "must-not-survive" not in str(stored_payload)

    with pytest.raises(Exception, match="append-only|permission denied"):
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            await connection.execute(
                text("UPDATE audit_records SET action='tampered' WHERE id=:id"),
                {"id": audit_id},
            )

    with pytest.raises(Exception, match="append-only|permission denied"):
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            await connection.execute(
                text("DELETE FROM audit_records WHERE id=:id"),
                {"id": audit_id},
            )
