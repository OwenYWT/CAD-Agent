"""M1 durable control-plane infrastructure contracts.

Hermetic tests validate configuration, readiness aggregation, and compose
topology. Real PostgreSQL, S3, and Temporal round trips are exercised separately
after the services are started; fakes in this file are not milestone evidence.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import Settings


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"

pytestmark = pytest.mark.unit


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "app_environment": "development",
        "durable_control_plane_enabled": True,
        "database_url": "postgresql+asyncpg://cad_agent:local-secret@postgres:5432/cad_agent",
        "object_store_endpoint_url": "http://minio:9000",
        "object_store_access_key": "cad-agent-local",
        "object_store_secret_key": "local-secret-not-for-production",
        "object_store_bucket": "cad-agent-artifacts",
        "temporal_target": "temporal:7233",
        "temporal_namespace": "default",
        "temporal_task_queue": "cad-agent-mcad",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"database_url": ""}, "DATABASE_URL"),
        ({"database_url": "sqlite:///data.db"}, "postgresql+asyncpg"),
        ({"object_store_endpoint_url": ""}, "OBJECT_STORE_ENDPOINT_URL"),
        ({"object_store_access_key": ""}, "OBJECT_STORE_ACCESS_KEY"),
        ({"object_store_secret_key": ""}, "OBJECT_STORE_SECRET_KEY"),
        ({"object_store_bucket": ""}, "OBJECT_STORE_BUCKET"),
        ({"temporal_target": ""}, "TEMPORAL_TARGET"),
        ({"temporal_namespace": ""}, "TEMPORAL_NAMESPACE"),
        ({"temporal_task_queue": ""}, "TEMPORAL_TASK_QUEUE"),
    ],
)
def test_enabled_durable_control_plane_requires_every_dependency(override, expected):
    problems = _settings(**override).durable_control_plane_config_problems()

    assert any(expected in problem for problem in problems)


def test_development_requires_the_only_supported_durable_write_path():
    settings = _settings(
        durable_control_plane_enabled=False,
    )

    problems = settings.durable_control_plane_config_problems()

    assert any("DURABLE_CONTROL_PLANE_ENABLED" in problem for problem in problems)


def test_production_fails_closed_when_durable_control_plane_is_disabled():
    settings = _settings(
        app_environment="production",
        durable_control_plane_enabled=False,
    )

    problems = settings.durable_control_plane_config_problems()

    assert any("DURABLE_CONTROL_PLANE_ENABLED" in problem for problem in problems)


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (
            {"object_store_endpoint_url": "http://minio.example.com:9000"},
            "HTTPS",
        ),
        (
            {"object_store_secret_key": "change-me"},
            "placeholder",
        ),
        (
            {"object_store_access_key": "<MINIO_ROOT_USER>"},
            "placeholder",
        ),
        (
            {"database_url": "postgresql+asyncpg://cad_agent:change-me@db:5432/cad_agent"},
            "placeholder",
        ),
        (
            {"database_url": "postgresql+asyncpg://cad_agent:<PASSWORD>@db:5432/cad_agent"},
            "placeholder",
        ),
        (
            {"database_url": "postgresql+asyncpg://cad_agent@db:5432/cad_agent"},
            "database password",
        ),
    ],
)
def test_production_rejects_insecure_or_placeholder_dependency_config(
    override,
    expected,
):
    settings = _settings(app_environment="production", **override)

    problems = settings.durable_control_plane_config_problems()

    assert any(expected in problem for problem in problems)


def test_production_accepts_complete_https_dependency_config():
    settings = _settings(
        app_environment="production",
        object_store_endpoint_url="https://objects.example.com",
    )

    assert settings.durable_control_plane_config_problems() == []


def test_durable_credentials_are_not_rendered_in_settings_repr():
    settings = _settings(
        database_url="postgresql+asyncpg://cad_agent:unique-db-secret@db:5432/cad_agent",
        object_store_access_key="unique-access-key",
        object_store_secret_key="unique-object-secret",
    )

    rendered = repr(settings)

    assert "unique-db-secret" not in rendered
    assert "unique-access-key" not in rendered
    assert "unique-object-secret" not in rendered


@pytest.mark.asyncio
async def test_readiness_reports_each_real_dependency(monkeypatch):
    from app import main

    async def database():
        return {"status": "ready", "latency_ms": 1}

    async def object_store():
        return {"status": "ready", "latency_ms": 2}

    async def temporal():
        return {"status": "ready", "latency_ms": 3}

    async def temporal_worker():
        return {
            "status": "ready",
            "latency_ms": 4,
            "workflow_pollers": 1,
            "activity_pollers": 1,
        }

    monkeypatch.setattr(main.settings, "durable_control_plane_enabled", True)
    monkeypatch.setattr(main, "database_readiness", database)
    monkeypatch.setattr(main, "object_store_readiness", object_store)
    monkeypatch.setattr(main, "temporal_readiness", temporal)
    monkeypatch.setattr(
        main,
        "temporal_worker_readiness",
        temporal_worker,
    )
    monkeypatch.setattr(
        main,
        "temporal_agent_v2_worker_readiness",
        temporal_worker,
    )

    result = await main._durable_control_plane_readiness()

    assert result["status"] == "ready"
    assert result["dependencies"] == {
        "postgresql": {"status": "ready", "latency_ms": 1},
        "object_store": {"status": "ready", "latency_ms": 2},
        "temporal": {"status": "ready", "latency_ms": 3},
        "temporal_worker": {
            "status": "ready",
            "latency_ms": 4,
            "workflow_pollers": 1,
            "activity_pollers": 1,
        },
        "temporal_agent_v2_worker": {
            "status": "ready",
            "latency_ms": 4,
            "workflow_pollers": 1,
            "activity_pollers": 1,
        },
    }
    assert result["problems"] == []


@pytest.mark.asyncio
async def test_readiness_requires_v2_worker_before_fusion_routing(monkeypatch):
    from app import main

    async def healthy():
        return {"status": "ready"}

    async def v2_unavailable():
        raise RuntimeError("no V2 poller")

    monkeypatch.setattr(main.settings, "durable_control_plane_enabled", True)
    monkeypatch.setattr(main, "database_readiness", healthy)
    monkeypatch.setattr(main, "object_store_readiness", healthy)
    monkeypatch.setattr(main, "temporal_readiness", healthy)
    monkeypatch.setattr(main, "temporal_worker_readiness", healthy)
    monkeypatch.setattr(
        main,
        "temporal_agent_v2_worker_readiness",
        v2_unavailable,
    )

    result = await main._durable_control_plane_readiness()

    assert result["status"] == "degraded"
    assert result["dependencies"]["temporal_agent_v2_worker"]["status"] == (
        "unavailable"
    )


@pytest.mark.asyncio
async def test_readiness_is_fail_closed_and_sanitizes_dependency_error(monkeypatch):
    from app import main

    async def database():
        raise RuntimeError(
            "could not connect postgresql://cad_agent:super-secret@db/cad_agent"
        )

    async def healthy():
        return {"status": "ready", "latency_ms": 1}

    monkeypatch.setattr(main.settings, "durable_control_plane_enabled", True)
    monkeypatch.setattr(main, "database_readiness", database)
    monkeypatch.setattr(main, "object_store_readiness", healthy)
    monkeypatch.setattr(main, "temporal_readiness", healthy)
    monkeypatch.setattr(main, "temporal_worker_readiness", healthy)

    result = await main._durable_control_plane_readiness()

    assert result["status"] == "degraded"
    assert result["dependencies"]["postgresql"]["status"] == "unavailable"
    rendered = str(result)
    assert "super-secret" not in rendered
    assert "postgresql" in result["problems"][0]


def test_compose_declares_durable_services_with_healthchecks_and_volumes():
    services = yaml.safe_load(COMPOSE.read_text())["services"]

    for name in (
        "postgres",
        "minio",
        "minio-init",
        "temporal",
        "migrate",
        "workflow-worker",
    ):
        assert name in services
    for name in ("postgres", "minio", "temporal"):
        assert services[name].get("healthcheck"), name

    volumes = yaml.safe_load(COMPOSE.read_text())["volumes"]
    assert "postgres_data" in volumes
    assert "minio_data" in volumes

    assert services["migrate"]["command"] == [
        "alembic",
        "upgrade",
        "head",
    ]
    assert services["workflow-worker"]["command"] == [
        "python",
        "-m",
        "app.workers.workflow_worker",
    ]
    compose_text = COMPOSE.read_text()
    assert "DURABLE_API_CUTOVER_ENABLED" not in compose_text
    assert "DURABLE_AGENT_FUSION_ENABLED" not in compose_text


def test_compose_requires_operator_supplied_local_credentials():
    text = COMPOSE.read_text()

    assert "${POSTGRES_PASSWORD:?" in text
    assert "${DATABASE_URL:?" in text
    assert "${MINIO_ROOT_USER:?" in text
    assert "${MINIO_ROOT_PASSWORD:?" in text
    assert "change-me" not in text


def test_pinned_control_plane_dependencies_are_declared():
    requirements = (ROOT / "backend" / "requirements.txt").read_text()

    for dependency in (
        "sqlalchemy==",
        "asyncpg==",
        "alembic==",
        "boto3==",
        "temporalio==",
    ):
        assert dependency in requirements.lower()
