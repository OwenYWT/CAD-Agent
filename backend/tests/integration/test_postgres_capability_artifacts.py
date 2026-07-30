"""Real PostgreSQL/MinIO persistence for CAD capability artifacts."""
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
from app.domain.identity import user_principal
from app.principal_context import bind_principal
from app.repositories.identity import reconcile_principal
from app.storage.capability_artifacts import commit, get, hydrate_reference


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not settings.object_store_endpoint_url,
    reason="real PostgreSQL and S3-compatible storage are required",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    if not TEST_DATABASE_URL:
        yield
        return
    original = (
        settings.database_url,
        settings.durable_control_plane_enabled,
    )
    settings.database_url = TEST_DATABASE_URL
    settings.durable_control_plane_enabled = True
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield
    (
        settings.database_url,
        settings.durable_control_plane_enabled,
    ) = original


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def database_engine_lifecycle(migrated_database):
    yield
    await close_database()


@pytest.mark.asyncio(loop_scope="module")
async def test_capability_artifact_survives_local_cache_loss(tmp_path):
    suffix = uuid4().hex
    request_id = f"upload-{suffix}"
    filename = "fixture.step"
    context = await reconcile_principal(
        user_principal(f"capability-{suffix}")
    )
    bind_principal(context)
    source = tmp_path / filename
    payload = b"ISO-10303-21;\nEND-ISO-10303-21;\n"
    source.write_bytes(payload)

    first = await commit(
        scope="uploads",
        request_id=request_id,
        filename=filename,
        path=source,
        content_type="application/step",
    )
    second = await commit(
        scope="uploads",
        request_id=request_id,
        filename=filename,
        path=source,
        content_type="application/step",
    )
    assert first["sha256"] == second["sha256"]
    source.write_bytes(payload + b"changed")
    with pytest.raises(ValueError):
        await commit(
            scope="uploads",
            request_id=request_id,
            filename=filename,
            path=source,
            content_type="application/step",
        )

    owner_root = tmp_path / "owner"
    await hydrate_reference(
        f"uploads/{request_id}/{filename}",
        owner_root,
    )
    assert (owner_root / "uploads" / request_id / filename).read_bytes() == payload
    assert await get("uploads", request_id, filename) is not None

    intruder = await reconcile_principal(
        user_principal(f"capability-intruder-{suffix}")
    )
    bind_principal(intruder)
    assert await get("uploads", request_id, filename) is None
