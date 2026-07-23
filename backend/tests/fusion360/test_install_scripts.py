import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts" / "fusion360"


@pytest.mark.parametrize(
    "name",
    ["install.sh", "start-runtime.sh", "stop-runtime.sh", "debug-runtime.sh", "upgrade.sh", "uninstall.sh"],
)
def test_shell_scripts_are_syntax_valid_and_dry_run_without_writes(name, tmp_path):
    script = SCRIPTS / name
    syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr
    env = {**os.environ, "HOME": str(tmp_path), "CAD_AGENT_FUSION_STATE_DIR": str(tmp_path / "state")}
    dry_run = subprocess.run(["bash", str(script), "--dry-run"], env=env, capture_output=True, text=True)
    assert dry_run.returncode == 0, dry_run.stderr
    assert "DRY-RUN" in dry_run.stdout
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    "name",
    ["install.ps1", "start-runtime.ps1", "stop-runtime.ps1", "debug-runtime.ps1", "upgrade.ps1", "uninstall.ps1"],
)
def test_powershell_scripts_have_whatif_and_no_hardcoded_secret(name):
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    assert "WhatIf" in text
    assert "FUSION_RUNTIME_BACKEND_SECRET=\"" not in text
    assert "FUSION_RUNTIME_CONNECTOR_SECRET=\"" not in text


def test_runtime_dependencies_are_exactly_pinned():
    lines = [line for line in (SCRIPTS / "runtime-requirements.txt").read_text().splitlines() if line]
    assert lines
    assert all("==" in line for line in lines)


def test_installers_default_to_direct_cloud_and_current_autodesk_addin_path(tmp_path):
    shell = (SCRIPTS / "install.sh").read_text(encoding="utf-8")
    powershell = (SCRIPTS / "install.ps1").read_text(encoding="utf-8")
    assert "direct HTTPS Cloud Agent mode" in shell
    assert "Autodesk/Autodesk Fusion/API/AddIns" in shell
    assert "--with-local-runtime" in shell
    assert "direct HTTPS Cloud Agent mode" in powershell
    assert "Autodesk\\Autodesk Fusion\\API\\AddIns" in powershell
    assert "WithLocalRuntime" in powershell

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "CAD_AGENT_FUSION_STATE_DIR": str(tmp_path / "state"),
    }
    result = subprocess.run(
        ["bash", str(SCRIPTS / "install.sh"), "--dry-run", "--with-local-runtime"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Optional loopback Runtime: enabled" in result.stdout
    assert not (tmp_path / "state").exists()


def test_addin_manifest_matches_autodesk_manifest_shape():
    path = ROOT / "fusion_addin" / "CADAgentFusionConnector" / "CADAgentFusionConnector.manifest"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["autodeskProduct"] == "Fusion360"
    assert manifest["type"] == "addin"
    assert str(uuid.UUID(manifest["id"])) == manifest["id"]
    assert manifest["description"][""]
    assert manifest["supportedOS"] == "windows|mac"
