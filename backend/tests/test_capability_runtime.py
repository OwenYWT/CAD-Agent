from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.capabilities.artifacts import ArtifactPathError, ArtifactStore, sha256_file
from app.capabilities.runtime import CapabilityRuntime, RuntimeConfig
from app.capabilities.registry import list_capabilities


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def make_runtime(workspace: Path, **overrides) -> CapabilityRuntime:
    values = {
        "repo_root": REPO_ROOT,
        "workspace_root": workspace,
        "artifact_root": workspace / ".artifacts",
        "timeout_seconds": 5,
    }
    values.update(overrides)
    return CapabilityRuntime(RuntimeConfig(**values))


def completed(stdout: bytes = b'{"ok": true}\n', stderr: bytes = b"", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_artifact_store_confines_request_paths_and_hashes(workspace: Path) -> None:
    store = ArtifactStore(workspace / "artifacts", workspace_root=workspace)
    with pytest.raises(ArtifactPathError):
        store.output_path("req-1", "../../escape.step")
    with pytest.raises(ArtifactPathError):
        store.request_dir("../req-1")

    metadata = store.write_bytes("req-1", "part.step", b"STEP DATA", suffixes={".step"})
    assert metadata["sha256"] == sha256_file(metadata["path"])
    assert metadata["relative_path"] == "req-1/part.step"


def test_unknown_parameters_cannot_supply_an_arbitrary_command(workspace: Path, monkeypatch) -> None:
    runtime = make_runtime(workspace)
    called = False

    def should_not_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess must not be called")

    monkeypatch.setattr("app.capabilities.runtime.subprocess.run", should_not_run)
    result = runtime.execute(
        "step-parts",
        "search",
        {"query": "M3 bolt", "argv": ["sh", "-c", "touch /tmp/pwned"]},
        request_id="req-command",
    )
    assert result["status"] == "failed"
    assert "unsupported parameter" in result["error"]
    assert called is False


def test_shell_metacharacters_stay_in_one_argument_and_shell_is_false(workspace: Path, monkeypatch) -> None:
    runtime = make_runtime(workspace)
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return completed(stdout=b'{"items": []}')

    monkeypatch.setattr("app.capabilities.runtime.subprocess.run", fake_run)
    query = "M3 bolt; touch /tmp/cad-agent-injection"
    result = runtime.execute("step-parts", "search", {"query": query}, request_id="req-injection")

    assert result["status"] == "succeeded"
    assert captured["shell"] is False
    assert query in captured["command"]
    assert "sh" not in captured["command"]
    assert "-c" not in captured["command"]


def test_positional_query_cannot_smuggle_an_origin_option(workspace: Path, monkeypatch) -> None:
    runtime = make_runtime(workspace)
    monkeypatch.setattr(
        "app.capabilities.runtime.subprocess.run",
        lambda *args, **kwargs: pytest.fail("option-like query must be rejected before subprocess"),
    )
    result = runtime.execute(
        "step-parts", "search", {"query": "--origin=https://attacker.invalid"}, request_id="req-origin"
    )
    assert result["status"] == "failed"
    assert "option prefix" in result["error"]


def test_generator_is_blocked_without_deployment_isolator(workspace: Path, monkeypatch) -> None:
    source = workspace / "robot.py"
    source.write_text("raise RuntimeError('must never run on host')\n", encoding="utf-8")
    runtime = make_runtime(workspace)

    monkeypatch.setattr(
        "app.capabilities.runtime.subprocess.run",
        lambda *args, **kwargs: pytest.fail("generator subprocess must be blocked"),
    )
    result = runtime.execute("urdf", "generate", {"source": "robot.py"}, request_id="req-generator")

    assert result["status"] == "blocked"
    assert "isolated executor" in result["blocked_reasons"][0]
    assert "robot.py" in " ".join(result["command_preview"])


def test_implicit_3mf_export_is_allowlisted_but_still_isolated(workspace: Path) -> None:
    source = workspace / "part.implicit.mjs"
    source.write_text("export default {}\n", encoding="utf-8")
    runtime = make_runtime(workspace)
    result = runtime.execute(
        "implicit-cad",
        "export",
        {"input": source.name, "format": "3mf", "output": "part.3mf"},
        request_id="req-implicit",
    )
    assert result["status"] == "blocked"
    assert "--format" in result["command_preview"]
    assert "3mf" in result["command_preview"]


def test_bambu_default_deployment_gate_wins_even_with_request_confirmation(workspace: Path, monkeypatch) -> None:
    runtime = make_runtime(workspace)
    monkeypatch.setattr(
        "app.capabilities.runtime.subprocess.run",
        lambda *args, **kwargs: pytest.fail("default runtime must never contact a printer"),
    )
    result = runtime.execute(
        "bambu-labs",
        "status",
        {"execute": True, "confirm_status": True, "host": "192.168.1.20", "access_code": "top-secret"},
        request_id="req-bambu-disabled",
    )
    assert result["status"] == "blocked"
    assert "server configuration" in result["blocked_reasons"][0]
    assert "top-secret" not in json.dumps(result)


def test_bambu_requires_execute_and_action_confirmation_when_deployed(workspace: Path, monkeypatch) -> None:
    runtime = make_runtime(workspace, allow_bambu_lan=True)
    monkeypatch.setattr(
        "app.capabilities.runtime.subprocess.run",
        lambda *args, **kwargs: pytest.fail("confirmation gate should block subprocess"),
    )
    no_execute = runtime.execute(
        "bambu-labs", "pause", {"confirm_pause_print": True}, request_id="req-pause-1"
    )
    no_confirm = runtime.execute(
        "bambu-labs", "pause", {"execute": True}, request_id="req-pause-2"
    )
    assert no_execute["status"] == "blocked"
    assert no_confirm["status"] == "blocked"


def test_bambu_access_code_is_redacted_from_result(workspace: Path, monkeypatch) -> None:
    runtime = make_runtime(workspace, allow_bambu_lan=True)
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return completed(stdout=b'{"status": "ok"}', stderr=b"")

    monkeypatch.setattr("app.capabilities.runtime.subprocess.run", fake_run)
    secret = "printer-access-123"
    result = runtime.execute(
        "bambu-labs",
        "status",
        {
            "execute": True,
            "confirm_status": True,
            "host": "192.168.1.20",
            "serial": "ABC123",
            "access_code": secret,
        },
        request_id="req-bambu-redact",
    )
    assert result["status"] == "succeeded"
    assert secret in captured["command"]  # required by the vendored CLI, but never returned/logged
    assert secret not in json.dumps(result)
    assert "[REDACTED]" in result["command_preview"]


def test_gcode_dry_run_builds_fixed_safe_command(workspace: Path, monkeypatch) -> None:
    mesh = workspace / "part.stl"
    mesh.write_bytes(b"solid empty\nendsolid empty\n")
    profile = workspace / "printer.json"
    profile.write_text("{}\n", encoding="utf-8")
    runtime = make_runtime(workspace)
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return completed(stdout=b'{"dry_run": true}')

    monkeypatch.setattr("app.capabilities.runtime.subprocess.run", fake_run)
    result = runtime.execute(
        "gcode",
        "dry-run",
        {"input": "part.stl", "profile": "printer.json", "output": "part.gcode", "backend": "auto"},
        request_id="req-gcode",
    )
    command = captured["command"]
    assert result["status"] == "dry_run"
    assert captured["shell"] is False
    assert "slice" in command
    assert "--dry-run" in command
    assert "--execute" not in command
    assert command[command.index("--output") + 1].startswith(str(workspace / ".artifacts" / "req-gcode"))


def test_missing_external_executable_returns_structured_blocked(workspace: Path, monkeypatch) -> None:
    runtime = make_runtime(workspace)
    monkeypatch.setattr(
        "app.capabilities.runtime.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    result = runtime.execute("gcode", "discover", {}, request_id="req-missing")
    assert result["status"] == "blocked"
    assert result["blocked_reasons"]


def test_every_manifest_action_has_a_runtime_handler(workspace: Path) -> None:
    runtime = make_runtime(workspace)
    missing = [
        f"{manifest.id}/{action.id}"
        for manifest in list_capabilities()
        for action in manifest.actions
        if (manifest.id, action.id) not in runtime._dispatch
    ]
    assert missing == []


def test_sendcutsend_preflight_refreshes_evidence_without_claiming_readiness(workspace: Path, monkeypatch) -> None:
    runtime = make_runtime(workspace)
    seen_urls = []

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, maximum: int) -> bytes:
            return self.payload[:maximum]

    def fake_urlopen(request, timeout):
        seen_urls.append(request.full_url)
        if request.full_url.endswith(".md"):
            return Response(b"# Official ordering guide\n")
        return Response(b'{"_meta":{"schema_version":"test"}}')

    monkeypatch.setattr("app.capabilities.runtime.urllib.request.urlopen", fake_urlopen)
    result = runtime.execute("sendcutsend", "preflight", {}, request_id="req-scs")

    assert result["status"] == "blocked"
    assert len(result["files"]) == 3
    assert all(url.startswith("https://cdn.sendcutsend.com/specs/") for url in seen_urls)
    assert "readiness verdict" in result["blocked_reasons"][0]


def test_sendcutsend_preflight_inspects_exact_dxf_and_writes_report(workspace: Path, monkeypatch) -> None:
    import ezdxf

    drawing = ezdxf.new()
    drawing.units = ezdxf.units.MM
    drawing.modelspace().add_lwpolyline(
        [(0, 0), (20, 0), (20, 10), (0, 10)],
        close=True,
    )
    drawing.saveas(workspace / "bracket.dxf")
    runtime = make_runtime(workspace)

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, maximum: int) -> bytes:
            return self.payload[:maximum]

    def fake_urlopen(request, timeout):
        if request.full_url.endswith(".md"):
            return Response(b"# Official ordering guide\n")
        return Response(b'{"materials":[{"sku":"AL-5052-1.5","name":"5052 aluminum"}]}')

    monkeypatch.setattr("app.capabilities.runtime.urllib.request.urlopen", fake_urlopen)
    result = runtime.execute(
        "sendcutsend",
        "preflight",
        {"input": "bracket.dxf", "material_sku": "AL-5052-1.5", "thickness_mm": 1.5},
        request_id="req-scs-dxf",
    )

    assert result["status"] == "succeeded"
    assert result["data"]["ready"] is False
    assert result["data"]["facts"]["dxf"]["insunits"] == ezdxf.units.MM
    assert result["data"]["facts"]["material_sku_matches"]
    assert any(check["name"] == "dxf_units" and check["status"] == "pass" for check in result["checks"])
    report = next(item for item in result["files"] if item["name"] == "sendcutsend-preflight.json")
    assert Path(report["path"]).is_file()
