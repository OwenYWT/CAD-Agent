"""Local stand-in for the containerised CadQuery executor, for measurement only.

NOT PART OF THE PRODUCT PIPELINE. Nothing under ``app/`` imports this module and
nothing ever should. The product deliberately refuses to execute generated CAD
on the host (CLAUDE.md: the business layer must not invoke Docker/Podman/host
shell, and a missing runtime must return ``blocked`` rather than fall back).
This harness exists so accuracy can be measured on a machine with no container
runtime; it replaces the isolation boundary here and only here.

If a container runtime IS available, prefer the real harness -- build the
sandbox image and run ``python -m benchmark.eval`` -- which exercises the
product's own executor instead of this one.

Everything else in the run is real: real planner, real code generation, real
CadQuery kernel, real geometry validation, real vision gate, repo scoring.

Each execution gets its own process with a hard timeout. That is not a detail:
generated CAD hangs inside OCP booleans often enough that an in-process runner
deadlocks a whole evaluation, because a Python thread cannot be cancelled. Only
a process can be killed for it -- which is one of the reasons the product runs
this in a container to begin with.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_CHILD = Path(__file__).parent / "exec_child.py"


@dataclass
class LocalSandboxResult:
    """Mirrors app.sandbox.executor.SandboxResult."""

    success: bool
    files: dict
    error_type: str | None
    error_message: str | None
    traceback: str | None
    execution_time_ms: int
    work_dir: Path


class LocalCadQueryExecutor:
    def __init__(
        self,
        *,
        timeout_s: int = 90,
        root: Path | None = None,
        max_concurrent: int = 4,
    ) -> None:
        self._timeout_s = timeout_s
        self._root = Path(root) if root else Path(tempfile.gettempdir()) / "cad-eval"
        self._root.mkdir(parents=True, exist_ok=True)
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def execute(
        self,
        code: str,
        mode: str = "3d",
        extra_files: dict | None = None,
        timeout_s: int | None = None,
        task: dict | None = None,
        resource_limits=None,
    ) -> LocalSandboxResult:
        async with self._semaphore:
            return await asyncio.to_thread(
                self._execute_sync, code, mode, extra_files, timeout_s
            )

    def _execute_sync(
        self,
        code: str,
        mode: str,
        extra_files: dict | None,
        timeout_s: int | None,
    ) -> LocalSandboxResult:
        started = time.time()
        work_dir = Path(tempfile.mkdtemp(prefix="cadeval_", dir=str(self._root)))
        (work_dir / "output").mkdir(parents=True, exist_ok=True)
        input_dir = work_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        for name, source in (extra_files or {}).items():
            try:
                shutil.copy2(source, input_dir / name)
            except Exception:
                pass

        limit = timeout_s or self._timeout_s
        try:
            completed = subprocess.run(
                [sys.executable, str(_CHILD), str(work_dir), mode],
                input=code,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=limit,
            )
        except subprocess.TimeoutExpired:
            return self._failure(
                work_dir,
                started,
                "TimeoutError",
                f"execution exceeded {limit}s and was killed",
            )

        result_path = work_dir / "output" / "result.json"
        if not result_path.exists():
            detail = (completed.stderr or completed.stdout or "").strip()[:1500]
            return self._failure(
                work_dir,
                started,
                "RuntimeError",
                f"execution produced no result.json. {detail}",
            )

        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return self._failure(
                work_dir, started, "RuntimeError", f"unreadable result.json: {exc}"
            )

        elapsed = int((time.time() - started) * 1000)
        if payload.get("status") == "success":
            files = {
                name: Path(path)
                for name, path in (payload.get("files") or {}).items()
                if Path(path).exists()
            }
            return LocalSandboxResult(
                success=True,
                files=files,
                error_type=None,
                error_message=None,
                traceback=None,
                execution_time_ms=elapsed,
                work_dir=work_dir,
            )
        return LocalSandboxResult(
            success=False,
            files={},
            error_type=payload.get("error_type"),
            error_message=payload.get("error_message"),
            traceback=payload.get("traceback"),
            execution_time_ms=elapsed,
            work_dir=work_dir,
        )

    @staticmethod
    def _failure(
        work_dir: Path, started: float, error_type: str, message: str
    ) -> LocalSandboxResult:
        return LocalSandboxResult(
            success=False,
            files={},
            error_type=error_type,
            error_message=message,
            traceback=None,
            execution_time_ms=int((time.time() - started) * 1000),
            work_dir=work_dir,
        )
