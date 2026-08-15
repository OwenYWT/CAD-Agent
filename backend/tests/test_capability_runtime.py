from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.capabilities import runtime as runtime_module
from app.capabilities.artifacts import ArtifactPathError, ArtifactStore, sha256_file
from app.capabilities.runtime import CapabilityRuntime, RuntimeConfig
from app.capabilities.registry import list_capabilities
from app.execution.backend import MaterializedExecutionOutcome
from app.execution.contracts import ExecutionResult, ExecutionStatus
from app.execution.podman_backend import RuntimeSnapshot
from app.execution.podman_backend import PodmanExecutionBackend
from app.dfm.step_analysis import StepAnalyzer


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def make_runtime(workspace: Path, **overrides) -> CapabilityRuntime:
    execution_backend = overrides.pop("execution_backend", None)
    values = {
        "repo_root": REPO_ROOT,
        "workspace_root": workspace,
        "artifact_root": workspace / ".artifacts",
        "timeout_seconds": 5,
    }
    values.update(overrides)
    return CapabilityRuntime(
        RuntimeConfig(**values),
        execution_backend=execution_backend,
    )


def completed(stdout: bytes = b'{"ok": true}\n', stderr: bytes = b"", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class RecordingExecutionBackend:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.specs = []

    def runtime_snapshot(self):
        return RuntimeSnapshot(
            image_digest=f"sha256:{'b' * 64}",
            platform="linux/arm64",
            versions={"cadquery": "2.8.0"},
        )

    async def execute(self, spec, **kwargs):
        self.specs.append((spec, kwargs))
        work = self.tmp_path / f"work-{len(self.specs)}"
        work.mkdir()
        artifact = work / "inspect.json"
        artifact.write_text('{"solids": 1}\n', encoding="utf-8")
        metadata = work / "capability-result.json"
        metadata.write_text(
            '{"exit_code":0,"result":{"solids":1},"stderr":null}',
            encoding="utf-8",
        )
        return MaterializedExecutionOutcome(
            result=ExecutionResult(
                execution_attempt_id=spec.execution_attempt_id,
                status=ExecutionStatus.SUCCEEDED,
            ),
            files={"artifact": artifact, "capability-result": metadata},
            work_dir=work,
        )


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

    monkeypatch.setattr("app.capabilities.runtime.host_process.run", should_not_run)
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

    monkeypatch.setattr("app.capabilities.runtime.host_process.run", fake_run)
    query = "M3 bolt; touch /tmp/cad-agent-injection"
    result = runtime.execute("step-parts", "search", {"query": query}, request_id="req-injection")

    assert result["status"] == "succeeded"
    assert query in captured["command"]
    assert "sh" not in captured["command"]
    assert "-c" not in captured["command"]


def test_positional_query_cannot_smuggle_an_origin_option(workspace: Path, monkeypatch) -> None:
    runtime = make_runtime(workspace)
    monkeypatch.setattr(
        "app.capabilities.runtime.host_process.run",
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
        "app.capabilities.runtime.host_process.run",
        lambda *args, **kwargs: pytest.fail("generator subprocess must be blocked"),
    )
    result = runtime.execute("urdf", "generate", {"source": "robot.py"}, request_id="req-generator")

    assert result["status"] == "blocked"
    assert "isolated executor" in result["blocked_reasons"][0]
    assert "robot.py" in " ".join(result["command_preview"])


@pytest.mark.asyncio
async def test_implicit_3mf_export_is_allowlisted_and_uses_backend(
    workspace: Path,
) -> None:
    source = workspace / "part.implicit.mjs"
    source.write_text("export default {}\n", encoding="utf-8")
    backend = RecordingExecutionBackend(workspace)
    runtime = make_runtime(workspace, execution_backend=backend)
    result = await runtime.execute_async(
        "implicit-cad",
        "export",
        {"input": source.name, "format": "3mf", "output": "part.3mf"},
        request_id="req-implicit",
    )
    assert result["status"] == "succeeded"
    spec = backend.specs[0][0]
    task = json.loads(spec.source.code)
    assert task["params"]["format"] == "3mf"
    assert task["params"]["output"] == "part.3mf"
    assert spec.outputs[0].media_type == "model/3mf"


@pytest.mark.asyncio
async def test_cad_inspect_uses_execution_backend_and_materializes_real_result(
    workspace: Path,
    monkeypatch,
) -> None:
    source = workspace / "part.step"
    source.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
    backend = RecordingExecutionBackend(workspace)
    runtime = make_runtime(workspace, execution_backend=backend)
    monkeypatch.setattr(
        "app.capabilities.runtime.host_process.run",
        lambda *args, **kwargs: pytest.fail("MCAD must not run as a host process"),
    )

    result = await runtime.execute_async(
        "cad",
        "inspect",
        {"input": source.name, "operation": "refs"},
        request_id="req-inspect",
    )

    assert result["status"] == "succeeded"
    assert result["data"]["result"] == {"solids": 1}
    assert result["files"][0]["name"] == "inspect.json"
    spec, call = backend.specs[0]
    assert spec.capability == "mcad.cad"
    assert spec.operation == "inspect"
    assert spec.source.language == "json"
    assert call["materialized_inputs"]


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_REAL_PODMAN") != "1",
    reason="set RUN_REAL_PODMAN=1 to exercise the actual unified MCAD runtime",
)
async def test_real_backend_runs_all_local_capability_families(
    workspace: Path,
) -> None:
    backend = PodmanExecutionBackend(os.environ["SANDBOX_IMAGE"])
    runtime = make_runtime(
        workspace,
        execution_backend=backend,
        timeout_seconds=120,
    )

    cad_source = workspace / "part.py"
    cad_source.write_text(
        "from build123d import Box, Cylinder\n\n"
        "def gen_step():\n"
        "    return Box(12, 8, 4) - Cylinder(2, 4)\n",
        encoding="utf-8",
    )
    generated = await runtime.execute_async(
        "cad",
        "step",
        {"input": "part.py", "output": "part.step", "force": True},
        request_id="real-cad-step",
    )
    assert generated["status"] == "succeeded", json.dumps(
        generated,
        ensure_ascii=False,
        indent=2,
    )
    step_path = Path(generated["files"][0]["path"])
    step_input = str(step_path.relative_to(workspace))

    dfm = await StepAnalyzer(execution_backend=backend).analyze(step_path)
    assert dfm.error is None, dfm.error
    assert dfm.global_properties.volume > 0
    assert dfm.global_properties.face_count == 7
    assert dfm.global_properties.edge_count == 30
    assert dfm.derived_metrics.min_hole_diameter == 4
    assert dfm.derived_metrics.min_fillet_radius is None

    inspected = await runtime.execute_async(
        "cad",
        "inspect",
        {"input": step_input, "operation": "refs"},
        request_id="real-cad-inspect",
    )
    assert inspected["status"] == "succeeded", json.dumps(
        inspected,
        ensure_ascii=False,
        indent=2,
    )
    assert isinstance(inspected["data"]["result"], dict)

    exported = await runtime.execute_async(
        "cad",
        "export",
        {"input": step_input, "format": "stl", "output": "part.stl", "force": True},
        request_id="real-cad-export",
    )
    assert exported["status"] == "succeeded", json.dumps(
        exported,
        ensure_ascii=False,
        indent=2,
    )
    assert Path(exported["files"][0]["path"]).suffix == ".stl"

    snapshot = await runtime.execute_async(
        "cad",
        "snapshot",
        {"input": step_input, "output": "part.png", "width": 320, "height": 240},
        request_id="real-cad-snapshot",
    )
    assert snapshot["status"] == "succeeded", snapshot
    assert Path(snapshot["files"][0]["path"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    dxf_source = workspace / "drawing.py"
    dxf_source.write_text(
        "import ezdxf\n\n"
        "def gen_dxf():\n"
        "    doc = ezdxf.new('R2010')\n"
        "    doc.modelspace().add_circle((0, 0), 10)\n"
        "    return doc\n",
        encoding="utf-8",
    )
    dxf = await runtime.execute_async(
        "dxf",
        "generate",
        {"source": "drawing.py", "output": "drawing.dxf"},
        request_id="real-dxf",
    )
    assert dxf["status"] == "succeeded", dxf
    assert Path(dxf["files"][0]["path"]).stat().st_size > 100

    implicit_source = workspace / "part.implicit.mjs"
    implicit_source.write_text(
        "export default {glsl: 'float sdf(vec3 p) { return length(p) - 1.0; }'};\n",
        encoding="utf-8",
    )
    implicit = await runtime.execute_async(
        "implicit-cad",
        "export",
        {
            "input": "part.implicit.mjs",
            "format": "stl",
            "output": "implicit.stl",
            "resolution": 32,
            "max_cells": 100000,
        },
        request_id="real-implicit",
    )
    assert implicit["status"] == "succeeded", implicit
    assert Path(implicit["files"][0]["path"]).stat().st_size > 100


def test_bambu_default_deployment_gate_wins_even_with_request_confirmation(workspace: Path, monkeypatch) -> None:
    runtime = make_runtime(workspace)
    monkeypatch.setattr(
        "app.capabilities.runtime.host_process.run",
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
        "app.capabilities.runtime.host_process.run",
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

    monkeypatch.setattr("app.capabilities.runtime.host_process.run", fake_run)
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

    monkeypatch.setattr("app.capabilities.runtime.host_process.run", fake_run)
    result = runtime.execute(
        "gcode",
        "dry-run",
        {"input": "part.stl", "profile": "printer.json", "output": "part.gcode", "backend": "auto"},
        request_id="req-gcode",
    )
    command = captured["command"]
    assert result["status"] == "dry_run"
    assert "slice" in command
    assert "--dry-run" in command
    assert "--execute" not in command
    assert command[command.index("--output") + 1].startswith(str(workspace / ".artifacts" / "req-gcode"))


def test_missing_external_executable_returns_structured_blocked(workspace: Path, monkeypatch) -> None:
    runtime = make_runtime(workspace)
    monkeypatch.setattr(
        "app.capabilities.runtime.host_process.run",
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
        if (
            (manifest.id, action.id) not in runtime._dispatch
            and (manifest.id, action.id) not in runtime_module._ASYNC_ACTIONS
        )
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
