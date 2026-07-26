from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from app.config import Settings
from app.sandbox.executor import CadQueryExecutor


ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "backend" / "sandbox"


def _runtime_lock() -> dict:
    return json.loads((SANDBOX / "runtime-lock.json").read_text(encoding="utf-8"))


def test_runtime_lock_covers_every_local_mcad_dependency() -> None:
    lock = _runtime_lock()

    assert lock["schema_version"] == "mcad-runtime-lock.v1"
    assert lock["python"]["version"] == "3.12.10"
    assert lock["node"]["version"] == "22.17.0"
    assert lock["browser"] == {
        "name": "chromium-headless-shell",
        "playwright_revision": "1223",
        "version": "148.0.7778.96",
    }

    required_python = {
        "build123d": "0.11.1",
        "cadquery": "2.8.0",
        "cadquery-ocp": "7.9.3.1.1",
        "cadquery-ocp-proxy": "7.9.3.1.1",
        "cadpy": "0.3.9",
        "ezdxf": "1.4.2",
        "numpy": "2.4.6",
        "pillow": "11.2.1",
        "playwright": "1.60.0",
        "trimesh": "4.12.2",
    }
    assert required_python.items() <= lock["python"]["packages"].items()
    required_node = {
        "gifenc": "1.0.3",
        "playwright": "1.60.0",
        "three": "0.160.0",
    }
    assert required_node.items() <= lock["node"]["packages"].items()
    assert set(lock["verified_operations"]) == {
        "cadquery_generate",
        "build123d_generate",
        "implicit_cad_export",
        "step_export",
        "stl_export",
        "dxf_export",
        "svg_export",
        "png_snapshot",
        "cad_inspect",
    }


def test_runtime_requirements_are_exactly_pinned_and_locked() -> None:
    lock = _runtime_lock()
    requirements = (SANDBOX / "runtime-requirements.txt").read_text(
        encoding="utf-8"
    )
    pinned = {}
    for raw_line in requirements.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==[^<>=!~\s]+", line), line
        name, version = line.split("==", maxsplit=1)
        pinned[name.lower()] = version

    locked = {
        name.lower(): version
        for name, version in lock["python"]["packages"].items()
    }
    separately_installed = {"build123d", "cadpy", "cadquery"}
    for name, version in locked.items():
        if name in separately_installed:
            continue
        assert pinned.get(name) == version


def test_runtime_base_images_are_immutable_multi_arch_references() -> None:
    lock = _runtime_lock()
    dockerfile = (SANDBOX / "Dockerfile").read_text(encoding="utf-8")

    for image in ("python_base", "node_base"):
        reference = lock["images"][image]
        assert re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", reference)
        assert reference in dockerfile

    assert "FROM docker.m.daocloud.io/condaforge/miniforge3:" not in dockerfile
    assert ":latest" not in dockerfile


def test_runtime_probe_and_skill_sources_are_bundled() -> None:
    dockerfile = (SANDBOX / "Dockerfile").read_text(encoding="utf-8")

    assert "runtime_probe.py" in dockerfile
    assert "runtime-lock.json" in dockerfile
    assert "patch_build123d.py" in dockerfile
    assert "third_party/cadskills/skills" in dockerfile
    assert "npm ci" in dockerfile
    assert "playwright install" in dockerfile


def test_executor_allows_both_supported_python_cad_apis() -> None:
    entry = (SANDBOX / "executor_entry.py").read_text(encoding="utf-8")

    assert '"cadquery"' in entry
    assert '"build123d"' in entry


def test_production_rejects_mutable_runtime_tag() -> None:
    runtime_settings = Settings(
        _env_file=None,
        app_environment="production",
        sandbox_image="registry.example/cad-agent-sandbox:latest",
        auth_required=False,
    )

    with pytest.raises(RuntimeError, match="immutable digest"):
        runtime_settings.assert_sandbox_config_safe()


def test_local_development_accepts_named_runtime_tag() -> None:
    runtime_settings = Settings(
        _env_file=None,
        app_environment="development",
        sandbox_image="cad-agent-sandbox:dev",
        auth_required=False,
    )

    runtime_settings.assert_sandbox_config_safe()


def test_production_accepts_immutable_runtime_digest() -> None:
    runtime_settings = Settings(
        _env_file=None,
        app_environment="production",
        sandbox_image=f"registry.example/cad-agent-sandbox@sha256:{'a' * 64}",
        auth_required=False,
    )

    runtime_settings.assert_sandbox_config_safe()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_REAL_PODMAN") != "1",
    reason="set RUN_REAL_PODMAN=1 to exercise the actual sandbox image",
)
async def test_real_sandbox_build123d_exports_step_and_stl() -> None:
    executor = CadQueryExecutor(
        runtime_name="podman",
        image_ref=os.environ["SANDBOX_IMAGE"],
    )

    result = await executor.execute(
        "from build123d import Box\n"
        "result = Box(12, 8, 4)\n",
        timeout_s=60,
    )

    assert result.success, result.error_message
    assert {path.suffix for path in result.files.values()} >= {".step", ".stl"}
    assert all(path.stat().st_size > 0 for path in result.files.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_error"),
    [
        ("open('/etc/passwd').read()\n", "open"),
        ("import socket\nsocket.socket()\n", "not allowed"),
        ("import subprocess\nsubprocess.run(['true'])\n", "not allowed"),
    ],
)
@pytest.mark.skipif(
    os.getenv("RUN_REAL_PODMAN") != "1",
    reason="set RUN_REAL_PODMAN=1 to exercise the actual sandbox image",
)
async def test_real_sandbox_blocks_file_network_and_process_access(
    code: str,
    expected_error: str,
) -> None:
    executor = CadQueryExecutor(
        runtime_name="podman",
        image_ref=os.environ["SANDBOX_IMAGE"],
    )

    result = await executor.execute(code, timeout_s=30)

    assert result.success is False
    assert expected_error in (result.error_message or "")
    assert result.files == {}
