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


@pytest.mark.asyncio(loop_scope="module")
async def test_explicit_check_configuration_ignores_ambient_identity_and_keeps_disabled_rules():
    from app.dfm.configuration import freeze_rules
    from app.dfm.postgres_store import configured_rules
    from app.principal_context import current_principal
    owners = [user_principal(f"check-rules-{uuid4()}") for _ in range(2)]
    configurations = []
    for index, owner in enumerate(owners):
        assert current_principal().tenant_id != owner.tenant_id
        async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
            await ensure_principal(connection, owner)
            await configured_rules(connection, tenant_id=owner.tenant_id, process="CNC")
            await connection.execute(text("""
                UPDATE dfm_rules SET threshold_max=:maximum, enabled=:enabled
                WHERE tenant_id=:tenant AND id='cnc_max_size'
            """), {"tenant": owner.tenant_id, "maximum": 1 if index == 0 else 1000,
                   "enabled": index == 0})
            rules = await configured_rules(connection, tenant_id=owner.tenant_id, process="cnc")
            configurations.append(freeze_rules(rules, tenant_id=owner.tenant_id,
                principal_id=owner.principal_id, process="cnc"))
    selected = [next(r for r in c['rules'] if r['id'] == 'cnc_max_size') for c in configurations]
    assert (selected[0]['threshold_max'], selected[0]['enabled']) == (1, True)
    assert (selected[1]['threshold_max'], selected[1]['enabled']) == (1000, False)
    assert configurations[0]['sha256'] != configurations[1]['sha256']


@pytest.mark.asyncio(loop_scope="module")
async def test_all_disabled_native_rules_do_not_silently_restore_builtin_rules():
    from app.dfm.postgres_store import configured_rules
    owner = user_principal(f"disabled-policy-{uuid4()}")
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        await ensure_principal(connection, owner)
        await configured_rules(connection, tenant_id=owner.tenant_id, process="CNC")
        await connection.execute(text("UPDATE dfm_rules SET enabled=false WHERE tenant_id=:tenant AND process='CNC'"), {"tenant": owner.tenant_id})
        policy = await resolve_dfm_policy_snapshot(connection, tenant_id=owner.tenant_id,
            manufacturing_profile={"process": "cnc", "material": "steel"})
    assert policy.source == "tenant-postgres" and policy.rules == ()
    assert policy.rule_set_versions and policy.threshold_precedence == "configured_rules"


@pytest.mark.asyncio(loop_scope="module")
async def test_legacy_check_captures_rules_once_without_rewriting_immutable_request():
    from app.dfm.postgres_store import configured_rules
    from app.repositories.projects import create_project
    from app.services.run_state import create_workflow
    from app.services.engineering_checks import execution_rule_configuration
    owner = user_principal(f"legacy-check-{uuid4()}")
    project = uuid4()
    original = {"process": "CNC", "description": "Legacy admitted check"}
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        await ensure_principal(connection, owner)
        await create_project(connection, project_id=project, tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id, name="Legacy check", slug=str(project))
        workflow = await create_workflow(connection, tenant_id=owner.tenant_id,
            project_id=project, requested_by_principal_id=owner.principal_id,
            kind="mcad.check", idempotency_key=str(uuid4()), request_payload=original)
        first = await execution_rule_configuration(connection, tenant_id=owner.tenant_id,
            principal_id=owner.principal_id, workflow_id=workflow.workflow_id,
            process="CNC", supplied=None)
        await connection.execute(text("UPDATE dfm_rules SET threshold_max=1 WHERE tenant_id=:tenant AND id='cnc_max_size'"), {"tenant": owner.tenant_id})
        second = await execution_rule_configuration(connection, tenant_id=owner.tenant_id,
            principal_id=owner.principal_id, workflow_id=workflow.workflow_id,
            process="CNC", supplied=None)
        assert first == second and first['origin'] == 'legacy_activity'
        assert await connection.scalar(text("SELECT request_payload FROM workflow_runs WHERE id=:id"), {"id": workflow.workflow_id}) == original
        assert await connection.scalar(text("SELECT count(*) FROM task_events WHERE workflow_run_id=:id AND event_type='engineering.configuration_captured'"), {"id": workflow.workflow_id}) == 1
        with pytest.raises(ValueError, match="ownership"):
            await execution_rule_configuration(connection, tenant_id=owner.tenant_id,
                principal_id=uuid4(), workflow_id=workflow.workflow_id, process="CNC", supplied=None)
