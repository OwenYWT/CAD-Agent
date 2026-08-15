"""Real PostgreSQL coverage for immutable tenant DFM policy snapshots."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.config import settings
from app.db import close_database, get_database_engine, tenant_transaction
from app.domain.identity import user_principal
from app.repositories.identity import ensure_principal
from app.validation.dfm_policy_snapshot import resolve_dfm_policy_snapshot


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
ORIGINAL_DATABASE_URL = settings.database_url
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="CAD_AGENT_TEST_DATABASE_URL is required for real PostgreSQL tests",
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
    async with get_database_engine().begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE knowledge_edges, knowledge_nodes, dfm_rules, "
                "dfm_rule_sets, principals, tenants CASCADE"
            )
        )
    yield


@pytest.mark.asyncio(loop_scope="module")
async def test_snapshot_freezes_tenant_rules_and_process_material_constraints():
    owner = user_principal(f"dfm-policy-{uuid4()}")
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        await ensure_principal(connection, owner)
        await connection.execute(
            text(
                "INSERT INTO dfm_rule_sets "
                "(tenant_id,id,name,process,version) "
                "VALUES (:tenant,'tenant-fdm','Tenant FDM','FDM','7')"
            ),
            {"tenant": owner.tenant_id},
        )
        await connection.execute(
            text(
                "INSERT INTO dfm_rules "
                "(tenant_id,id,rule_set_id,process,category,check_type,"
                "threshold_min,unit,severity) "
                "VALUES (:tenant,'wall','tenant-fdm','FDM','wall_thickness',"
                "'geometric',0.9,'mm','error')"
            ),
            {"tenant": owner.tenant_id},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_nodes "
                "(tenant_id,customer_id,id,type,name) VALUES "
                "(:tenant,'default','proc_fdm','process','FDM'),"
                "(:tenant,'default','mat_pla','material','PLA')"
            ),
            {"tenant": owner.tenant_id},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_edges "
                "(tenant_id,customer_id,source_id,target_id,relationship,properties) "
                "VALUES "
                "(:tenant,'default','proc_fdm','proc_fdm','has_constraint',"
                "CAST(:process_properties AS jsonb)),"
                "(:tenant,'default','proc_fdm','mat_pla','supports_material',"
                "CAST(:material_properties AS jsonb))"
            ),
            {
                "tenant": owner.tenant_id,
                "process_properties": '{"max_size":220}',
                "material_properties": '{"min_wall":1.1}',
            },
        )
        policy = await resolve_dfm_policy_snapshot(
            connection,
            tenant_id=owner.tenant_id,
            manufacturing_profile={"process": "fdm", "material": "PLA"},
        )

    assert policy.source == "tenant-postgres"
    assert policy.rule_set_versions == {"tenant-fdm": "7"}
    assert policy.rules[0].severity == "critical"
    assert policy.knowledge_constraints == {"max_size": 220, "min_wall": 1.1}
    assert len(policy.policy_hash) == 64


@pytest.mark.asyncio(loop_scope="module")
async def test_snapshot_rejects_unsupported_process_instead_of_silent_fallback():
    owner = user_principal(f"dfm-policy-invalid-{uuid4()}")
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        await ensure_principal(connection, owner)
        with pytest.raises(ValueError, match="unsupported manufacturing process"):
            await resolve_dfm_policy_snapshot(
                connection,
                tenant_id=owner.tenant_id,
                manufacturing_profile={"process": "unknown", "material": "X"},
            )
