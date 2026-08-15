"""Real tenant-scoped DFM and knowledge graph persistence."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config

from app.config import settings
from app.db import close_database
from app.dfm import knowledge_graph, rule_store
from app.dfm.knowledge_graph import KGEdge, KGNode
from app.domain.identity import user_principal
from app.principal_context import bind_principal
from app.repositories.identity import reconcile_principal


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
    original = (
        settings.database_url,
        settings.durable_control_plane_enabled,
        settings.history_db_path,
    )
    settings.database_url = TEST_DATABASE_URL
    settings.durable_control_plane_enabled = True
    settings.history_db_path = "/path-that-must-not-be-created/history.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield
    (
        settings.database_url,
        settings.durable_control_plane_enabled,
        settings.history_db_path,
    ) = original


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def database_engine_lifecycle(migrated_database):
    yield
    await close_database()


@pytest.mark.asyncio(loop_scope="module")
async def test_dfm_and_knowledge_are_seeded_mutable_and_tenant_isolated():
    suffix = uuid4().hex
    context = await reconcile_principal(
        user_principal(f"engineering-{suffix}")
    )
    bind_principal(context)

    sets = await rule_store.list_rule_sets()
    assert sets
    source = sets[0]
    clone_id = f"custom-{suffix}"
    clone = await rule_store.clone_rule_set(
        source.id,
        clone_id,
        "客户规则",
    )
    assert clone and clone.rules
    changed = await rule_store.update_rule(
        clone.rules[0].id,
        {"severity": "error", "enabled": False},
    )
    assert changed and changed.severity == "error" and not changed.enabled
    assert await rule_store.delete_rule_set(clone_id)
    assert not await rule_store.delete_rule_set(source.id)

    defaults = await knowledge_graph.list_nodes(customer_id="default")
    assert defaults
    customer = f"customer-{suffix}"
    assert await knowledge_graph.clone_for_customer(customer) == len(defaults)
    assert await knowledge_graph.clone_for_customer(customer) == 0
    custom_a = KGNode(
        id=f"process-{suffix}",
        type="process",
        name="定制工艺",
        customer_id=customer,
    )
    custom_b = KGNode(
        id=f"material-{suffix}",
        type="material",
        name="定制材料",
        customer_id=customer,
    )
    await knowledge_graph.upsert_node(custom_a)
    await knowledge_graph.upsert_node(custom_b)
    edge = await knowledge_graph.add_edge(
        KGEdge(
            source_id=custom_a.id,
            target_id=custom_b.id,
            relationship="supports_material",
            customer_id=customer,
        )
    )
    assert edge.id is not None
    assert await knowledge_graph.get_node(custom_a.id, customer) == custom_a
    assert await knowledge_graph.delete_edge(edge.id)
    assert await knowledge_graph.delete_node(custom_a.id, customer)

    intruder = await reconcile_principal(
        user_principal(f"engineering-intruder-{suffix}")
    )
    bind_principal(intruder)
    assert await knowledge_graph.list_nodes(customer_id=customer) == []
    intruder_sets = await rule_store.list_rule_sets()
    assert intruder_sets
    assert all(item.id != clone_id for item in intruder_sets)
