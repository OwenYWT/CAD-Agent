from app.models.schemas import GenerateResponse, RepairStep


def test_generate_response_serializes_repair_history():
    response = GenerateResponse(
        request_id="req-1",
        success=True,
        repair_history=[
            RepairStep(
                attempt=1,
                stage="execution",
                error_type="ExecutionError",
                message="CadQuery failed",
                action="fix_error",
                status="repaired",
            )
        ],
    )

    dumped = response.model_dump()
    assert dumped["repair_history"] == [
        {
            "attempt": 1,
            "stage": "execution",
            "error_type": "ExecutionError",
            "message": "CadQuery failed",
            "action": "fix_error",
            "status": "repaired",
        }
    ]


def test_generate_response_defaults_to_empty_repair_history():
    response = GenerateResponse(request_id="req-1", success=True)

    assert response.model_dump()["repair_history"] == []
    assert response.model_dump()["repair_history"] == []
from pathlib import Path
import builtins

import pytest

from app.agent.orchestrator import Orchestrator
from app.sandbox.executor import SandboxResult


def test_orchestrator_does_not_eagerly_import_vector_retriever(monkeypatch):
    original_import = builtins.__import__
    vector_imports = []

    def guarded_import(name, *args, **kwargs):
        if name == "app.examples.vector_retriever":
            vector_imports.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    orchestrator = Orchestrator()

    assert orchestrator._retriever is None
    assert vector_imports == []


class FakeExecutor:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.calls = 0

    async def execute(self, code: str, mode: str = "3d") -> SandboxResult:
        self.calls += 1
        work_dir = self.tmp_path / f"work_{self.calls}"
        (work_dir / "output").mkdir(parents=True)
        if self.calls == 1:
            return SandboxResult(
                success=False,
                files={},
                error_type="ValueError",
                error_message="CadQuery failed",
                traceback="Traceback...",
                execution_time_ms=10,
                work_dir=work_dir,
            )
        return SandboxResult(
            success=True,
            files={},
            error_type=None,
            error_message=None,
            traceback=None,
            execution_time_ms=20,
            work_dir=work_dir,
        )


class FakeCodeGen:
    async def fix_error(self, code, error, plan=None):
        return code + "\n# fixed"


@pytest.mark.asyncio
async def test_execute_with_retry_records_execution_repair_history(tmp_path):
    orchestrator = Orchestrator()
    orchestrator.MAX_RETRIES = 2
    orchestrator.executor = FakeExecutor(tmp_path)
    orchestrator.code_gen = FakeCodeGen()

    response = await orchestrator._execute_with_retry(
        request_id="req-1",
        code="import cadquery as cq\nresult = None\nshow_object(result)",
        plan=None,
        output_formats=["dxf"],
        on_step=None,
        user_prompt="",
        is_2d=True,
    )

    assert response.success is True
    assert response.attempts == 2
    assert response.model_dump()["repair_history"] == [
        {
            "attempt": 1,
            "stage": "execution",
            "error_type": "ValueError",
            "message": "CadQuery failed",
            "action": "fix_error",
            "status": "repaired",
        }
    ]
