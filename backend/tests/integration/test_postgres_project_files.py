"""Real local-output to PostgreSQL/MinIO compatibility commit."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config

from app.api.files import download_file
from app.config import settings
from app.db import close_database
from app.domain.identity import user_principal
from app.object_store import get_object
from app.principal_context import bind_principal
from app.repositories.identity import reconcile_principal
from app.storage.file_ownership import (
    FileOwnershipError,
    claim_request_owner,
    get_project_file,
    hydrate_project_files,
    request_belongs_to,
)


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not settings.object_store_endpoint_url,
    reason="real PostgreSQL and S3-compatible storage are required",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_database(tmp_path_factory):
    if not TEST_DATABASE_URL:
        yield
        return
    original = (
        settings.database_url,
        settings.durable_control_plane_enabled,
        settings.file_storage_dir,
    )
    settings.database_url = TEST_DATABASE_URL
    settings.durable_control_plane_enabled = True
    settings.file_storage_dir = str(tmp_path_factory.mktemp("project-files"))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield
    (
        settings.database_url,
        settings.durable_control_plane_enabled,
        settings.file_storage_dir,
    ) = original


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def database_engine_lifecycle(migrated_database):
    yield
    await close_database()


@pytest.mark.asyncio(loop_scope="module")
async def test_generated_bytes_commit_idempotently_and_download_after_local_delete():
    suffix = uuid4().hex
    user_id = f"artifact-user-{suffix}"
    request_id = f"request-{suffix}"
    context = await reconcile_principal(user_principal(user_id))
    bind_principal(context)
    directory = Path(settings.file_storage_dir) / request_id
    directory.mkdir(parents=True)
    local = directory / "result.step"
    payload = b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"
    local.write_bytes(payload)

    await claim_request_owner(request_id, f"user:{user_id}")
    await claim_request_owner(request_id, f"user:{user_id}")
    assert not (directory / ".owner").exists()
    assert await request_belongs_to(request_id, f"user:{user_id}")
    metadata = await get_project_file(request_id, "result.step")
    assert metadata and await get_object(metadata["object_key"]) == payload

    local.unlink()
    response = await download_file(
        request_id,
        "result.step",
        _credential=f"user:{user_id}",
    )
    assert response.body == payload
    assert response.headers["etag"] == f'"{metadata["sha256"]}"'
    hydrated = await hydrate_project_files(
        request_id,
        extensions={".step"},
    )
    assert hydrated == [local]
    assert local.read_bytes() == payload

    intruder = await reconcile_principal(
        user_principal(f"artifact-intruder-{suffix}")
    )
    bind_principal(intruder)
    assert not await request_belongs_to(request_id, "intruder")

    bind_principal(context)
    local.write_bytes(payload + b"changed")
    with pytest.raises(FileOwnershipError):
        await claim_request_owner(request_id, f"user:{user_id}")
