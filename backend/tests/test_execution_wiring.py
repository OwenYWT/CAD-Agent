from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from app.agent.orchestrator import Orchestrator
from app.agent.multi_step import MultiStepExecutor
from app.capabilities.runtime import CapabilityRuntime, RuntimeConfig
from app.dfm.step_analysis import StepAnalyzer
from app.execution.composition import get_execution_backend
from app.execution import host_process
from app.workflows.activities import McadWorkflowActivities


APP_ROOT = Path(__file__).resolve().parents[1] / "app"
BANNED_IMPORT_ROOTS = {"docker", "subprocess"}
BANNED_IMPORT_NAMES = {
    "app.sandbox.executor.CadQueryExecutor",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    return imported


def test_business_modules_do_not_import_container_or_process_implementations() -> None:
    roots = ("agent", "api", "dfm", "validation", "capabilities")
    violations: list[str] = []
    for root in roots:
        for path in sorted((APP_ROOT / root).rglob("*.py")):
            for imported in _imports(path):
                if imported.split(".", maxsplit=1)[0] in BANNED_IMPORT_ROOTS:
                    violations.append(f"{path.relative_to(APP_ROOT)} imports {imported}")
                if imported in BANNED_IMPORT_NAMES:
                    violations.append(f"{path.relative_to(APP_ROOT)} imports {imported}")
    assert violations == []


def test_application_composition_reuses_one_execution_backend() -> None:
    get_execution_backend.cache_clear()
    try:
        assert get_execution_backend() is get_execution_backend()
    finally:
        get_execution_backend.cache_clear()


def test_durable_worker_and_multi_step_share_the_composed_backend() -> None:
    get_execution_backend.cache_clear()
    try:
        backend = get_execution_backend()
        activities = McadWorkflowActivities()
        orchestrator = activities.source_preparer.orchestrator
        multi_step = MultiStepExecutor(
            orchestrator.code_gen,
            orchestrator.executor,
        )

        assert activities.backend is backend
        assert orchestrator.executor.backend is backend
        assert multi_step.executor.backend is backend
    finally:
        get_execution_backend.cache_clear()


def test_orchestrator_dfm_and_capabilities_accept_the_same_backend(tmp_path: Path) -> None:
    class RecordingBackend:
        pass

    backend = RecordingBackend()
    orchestrator = Orchestrator(execution_backend=backend)
    analyzer = StepAnalyzer(execution_backend=backend)
    runtime = CapabilityRuntime(
        RuntimeConfig(
            repo_root=Path(__file__).resolve().parents[2],
            workspace_root=tmp_path,
            artifact_root=tmp_path / "artifacts",
        ),
        execution_backend=backend,
    )

    assert orchestrator.executor.backend is backend
    assert analyzer.executor.backend is backend
    assert runtime.execution_backend is backend


def test_reviewed_host_process_runner_never_uses_a_shell(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(host_process.subprocess, "run", fake_run)
    host_process.run(
        ["connector-cli", "literal;not-shell"],
        cwd=tmp_path,
        timeout=3,
    )

    assert captured["command"] == ["connector-cli", "literal;not-shell"]
    assert captured["shell"] is False
    assert captured["check"] is False
