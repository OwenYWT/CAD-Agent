"""Real PostgreSQL isolation tests for every legacy-store replacement."""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.config import settings
from app.db import (
    auth_transaction,
    close_database,
    get_database_engine,
    tenant_transaction,
)
from app.domain.identity import user_principal
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.repositories.revisions import create_initial_branch


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
ORIGINAL_DATABASE_URL = settings.database_url
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="CAD_AGENT_TEST_DATABASE_URL is required for real PostgreSQL tests",
)

TENANT_TABLES = (
    "auth_users",
    "auth_sessions",
    "auth_verification_codes",
    "auth_invite_codes",
    "workspace_sessions",
    "workspace_panels",
    "workspace_messages",
    "legacy_snapshot_mappings",
    "project_files",
    "dfm_rule_sets",
    "dfm_rules",
    "knowledge_nodes",
    "knowledge_edges",
    "product_feedback",
    "connector_links",
    "connector_tokens",
    "connector_oauth_states",
    "connector_records",
    "connector_audit_records",
    "connector_artifacts",
    "capability_artifacts",
    "legacy_quarantine_records",
    "legacy_import_runs",
    "legacy_import_mappings",
)

RUNTIME_TABLES = (
    "workspace_sessions",
    "workspace_panels",
    "workspace_messages",
    "legacy_snapshot_mappings",
    "project_files",
    "dfm_rule_sets",
    "dfm_rules",
    "knowledge_nodes",
    "knowledge_edges",
    "product_feedback",
    "connector_links",
    "connector_tokens",
    "connector_oauth_states",
    "connector_records",
    "connector_audit_records",
    "connector_artifacts",
    "capability_artifacts",
)


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


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def database_engine_lifecycle(migrated_database):
    settings.database_url = TEST_DATABASE_URL
    yield
    await close_database()
    settings.database_url = ORIGINAL_DATABASE_URL


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean_database():
    if not TEST_DATABASE_URL:
        yield
        return
    tables = ", ".join((*reversed(TENANT_TABLES), "change_sets"))
    async with get_database_engine().begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} CASCADE"))
    yield


async def _seed_identity(label: str):
    context = user_principal(f"legacy-store-{label}-{uuid4()}")
    project_id = uuid4()
    async with tenant_transaction(
        context.tenant_id,
        context.principal_id,
    ) as connection:
        await ensure_principal(connection, context)
        await create_project(
            connection,
            project_id=project_id,
            tenant_id=context.tenant_id,
            creator_principal_id=context.principal_id,
            name=f"{label} project",
            slug=f"{label}-{project_id.hex[:10]}",
        )
        branch = await create_initial_branch(
            connection,
            tenant_id=context.tenant_id,
            project_id=project_id,
            created_by_principal_id=context.principal_id,
            branch_name="main",
            initial_manifest={"source": "legacy-store-rls-test"},
        )
    return context, project_id, branch


@pytest.mark.asyncio(loop_scope="module")
async def test_all_replacement_tables_force_tenant_rls_and_have_named_policies():
    async with get_database_engine().connect() as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                           count(p.policyname) AS policy_count
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    LEFT JOIN pg_policies p
                      ON p.schemaname=n.nspname AND p.tablename=c.relname
                    WHERE n.nspname='public' AND c.relname = ANY(:tables)
                    GROUP BY c.relname, c.relrowsecurity, c.relforcerowsecurity
                    """
                ),
                {"tables": list(TENANT_TABLES)},
            )
        ).mappings()
    by_name = {row["relname"]: row for row in rows}
    assert set(by_name) == set(TENANT_TABLES)
    assert all(row["relrowsecurity"] for row in by_name.values())
    assert all(row["relforcerowsecurity"] for row in by_name.values())
    assert all(int(row["policy_count"]) >= 1 for row in by_name.values())


@pytest.mark.asyncio(loop_scope="module")
async def test_runtime_rows_are_visible_only_inside_the_owning_tenant():
    owner, project_id, branch = await _seed_identity("owner")
    intruder, _, _ = await _seed_identity("intruder")
    session_id = f"session-{uuid4()}"
    panel_id = f"panel-{uuid4()}"
    request_id = f"request-{uuid4()}"
    now_payload = json.dumps({"ok": True}, separators=(",", ":"))

    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        await connection.execute(
            text(
                """
                INSERT INTO workspace_sessions (
                    id, tenant_id, project_id, created_by_principal_id, title
                ) VALUES (:id, :tenant, :project, :principal, 'Imported')
                """
            ),
            {
                "id": session_id,
                "tenant": owner.tenant_id,
                "project": project_id,
                "principal": owner.principal_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO connector_oauth_states (
                    tenant_id, principal_id, connector, state,
                    encrypted_verifier, expires_at
                ) VALUES (
                    :tenant, :principal, 'fusion360', 'oauth-state',
                    :verifier, CURRENT_TIMESTAMP + INTERVAL '5 minutes'
                )
                """
            ),
            {
                "tenant": owner.tenant_id,
                "principal": owner.principal_id,
                "verifier": b"encrypted-verifier",
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO workspace_panels (id, tenant_id, session_id, title)
                VALUES (:id, :tenant, :session, 'Model')
                """
            ),
            {"id": panel_id, "tenant": owner.tenant_id, "session": session_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO workspace_messages (
                    tenant_id, panel_id, role, content, result
                ) VALUES (:tenant, :panel, 'assistant', 'done', CAST(:result AS jsonb))
                """
            ),
            {
                "tenant": owner.tenant_id,
                "panel": panel_id,
                "result": now_payload,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO legacy_snapshot_mappings (
                    tenant_id, legacy_snapshot_id, panel_id, project_id,
                    revision_id, legacy_version, source, prompt, status
                ) VALUES (
                    :tenant, 'snapshot-1', :panel, :project,
                    :revision, 1, 'generate', 'make part', 'pass'
                )
                """
            ),
            {
                "tenant": owner.tenant_id,
                "panel": panel_id,
                "project": project_id,
                "revision": branch.revision_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO project_files (
                    id, tenant_id, project_id, revision_id, request_id,
                    filename, content_type, size_bytes, sha256, object_key, source
                ) VALUES (
                    :id, :tenant, :project, :revision, :request,
                    'part.step', 'application/step', 1, :sha, :object_key, 'legacy'
                )
                """
            ),
            {
                "id": uuid4(),
                "tenant": owner.tenant_id,
                "project": project_id,
                "revision": branch.revision_id,
                "request": request_id,
                "sha": "a" * 64,
                "object_key": f"tests/{owner.tenant_id}/{request_id}/part.step",
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO dfm_rule_sets (
                    tenant_id, id, name, process, version
                ) VALUES (:tenant, 'fdm', 'FDM', 'fdm', '1')
                """
            ),
            {"tenant": owner.tenant_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO dfm_rules (
                    tenant_id, id, rule_set_id, process, category, check_type,
                    severity
                ) VALUES (
                    :tenant, 'wall', 'fdm', 'FDM', 'geometry',
                    'geometric', 'error'
                )
                """
            ),
            {"tenant": owner.tenant_id},
        )
        for node_id in ("process", "material"):
            await connection.execute(
                text(
                    """
                    INSERT INTO knowledge_nodes (
                        tenant_id, customer_id, id, type, name
                    ) VALUES (:tenant, 'default', :id, 'process', :id)
                    """
                ),
                {"tenant": owner.tenant_id, "id": node_id},
            )
        await connection.execute(
            text(
                """
                INSERT INTO knowledge_edges (
                    tenant_id, customer_id, source_id, target_id,
                    relationship
                ) VALUES (
                    :tenant, 'default', 'process', 'material', 'supports'
                )
                """
            ),
            {"tenant": owner.tenant_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO product_feedback (
                    id, tenant_id, principal_id, project_id, request_id, rating
                ) VALUES (
                    :id, :tenant, :principal, :project, :request, 'up'
                )
                """
            ),
            {
                "id": uuid4(),
                "tenant": owner.tenant_id,
                "principal": owner.principal_id,
                "project": project_id,
                "request": request_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO connector_links (
                    tenant_id, principal_id, project_id, connector, request_id,
                    document_id, workspace_id, status, external_url, mode
                ) VALUES (
                    :tenant, :principal, :project, 'onshape', :request,
                    'doc', 'workspace', 'done', 'https://example.invalid/doc',
                    'import_step'
                )
                """
            ),
            {
                "tenant": owner.tenant_id,
                "principal": owner.principal_id,
                "project": project_id,
                "request": request_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO connector_tokens (
                    tenant_id, principal_id, connector, encrypted_token,
                    encryption_key_id
                ) VALUES (:tenant, :principal, 'fusion360', :token, 'test-key')
                """
            ),
            {
                "tenant": owner.tenant_id,
                "principal": owner.principal_id,
                "token": b"encrypted",
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO connector_records (
                    id, tenant_id, principal_id, connector, record_type,
                    external_id, state, payload
                ) VALUES (
                    :id, :tenant, :principal, 'fusion360', 'task',
                    :request, 'queued', CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "id": uuid4(),
                "tenant": owner.tenant_id,
                "principal": owner.principal_id,
                "request": request_id,
                "payload": now_payload,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO connector_audit_records (
                    id, tenant_id, principal_id, connector, request_id,
                    event_type, payload
                ) VALUES (
                    :id, :tenant, :principal, 'fusion360', :request,
                    'planned', CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "id": uuid4(),
                "tenant": owner.tenant_id,
                "principal": owner.principal_id,
                "request": request_id,
                "payload": now_payload,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO connector_artifacts (
                    id, tenant_id, principal_id, connector, request_id,
                    filename, content_type, size_bytes, sha256, object_key
                ) VALUES (
                    :id, :tenant, :principal, 'fusion360', :request,
                    'fusion.step', 'application/step', 1, :sha, :key
                )
                """
            ),
            {
                "id": uuid4(),
                "tenant": owner.tenant_id,
                "principal": owner.principal_id,
                "request": request_id,
                "sha": "b" * 64,
                "key": f"tests/{owner.tenant_id}/fusion/{request_id}",
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO capability_artifacts (
                    id, tenant_id, principal_id, project_id, scope, request_id,
                    filename, content_type, size_bytes, sha256, object_key
                ) VALUES (
                    :id, :tenant, :principal, :project, 'runs', :request,
                    'mesh.stl', 'application/sla', 1, :sha, :key
                )
                """
            ),
            {
                "id": uuid4(),
                "tenant": owner.tenant_id,
                "principal": owner.principal_id,
                "project": project_id,
                "request": request_id,
                "sha": "c" * 64,
                "key": f"tests/{owner.tenant_id}/capability/{request_id}",
            },
        )

    async with tenant_transaction(
        intruder.tenant_id,
        intruder.principal_id,
    ) as connection:
        for table in RUNTIME_TABLES:
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0


@pytest.mark.asyncio(loop_scope="module")
async def test_auth_role_is_narrow_and_runtime_cannot_read_auth_rows():
    owner, _, _ = await _seed_identity("auth-owner")
    async with auth_transaction() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO auth_users (
                    id, tenant_id, principal_id, phone, phone_lookup_hash,
                    password_hash, registered_via, last_login_at
                ) VALUES (
                    'user-1', :tenant, :principal, '+8613800000000',
                    :lookup, 'pbkdf2', 'invite', CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "tenant": owner.tenant_id,
                "principal": owner.principal_id,
                "lookup": "d" * 64,
            },
        )
        assert await connection.scalar(text("SELECT count(*) FROM auth_users")) == 1

    with pytest.raises(ProgrammingError):
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            await connection.scalar(text("SELECT count(*) FROM auth_users"))

    with pytest.raises(ProgrammingError):
        async with auth_transaction() as connection:
            await connection.scalar(text("SELECT count(*) FROM projects"))


@pytest.mark.asyncio(loop_scope="module")
async def test_quarantine_index_is_tenant_isolated_and_not_runtime_visible():
    owner, _, _ = await _seed_identity("quarantine-owner")
    intruder, _, _ = await _seed_identity("quarantine-intruder")
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
        role="migrator",
    ) as connection:
        await connection.execute(
            text(
                """
                INSERT INTO legacy_quarantine_records (
                    id, tenant_id, principal_id, source_fingerprint,
                    source_reference, reason
                ) VALUES (
                    :id, :tenant, :principal, :fingerprint,
                    'history.sessions:ownerless', 'legacy_owner_unresolved'
                )
                """
            ),
            {
                "id": uuid4(),
                "tenant": owner.tenant_id,
                "principal": owner.principal_id,
                "fingerprint": "e" * 64,
            },
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM legacy_quarantine_records")
            )
            == 1
        )

    async with tenant_transaction(
        intruder.tenant_id,
        intruder.principal_id,
        role="migrator",
    ) as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM legacy_quarantine_records")
            )
            == 0
        )

    with pytest.raises(ProgrammingError):
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            await connection.scalar(
                text("SELECT count(*) FROM legacy_quarantine_records")
            )
