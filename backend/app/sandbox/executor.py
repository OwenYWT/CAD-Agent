import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import docker

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class SandboxResult:
    success: bool
    files: dict[str, Path]
    error_type: str | None
    error_message: str | None
    traceback: str | None
    execution_time_ms: int
    work_dir: Path


class PodmanRuntime:
    def __init__(self):
        self.command = settings.sandbox_command or "podman"
        self._ensure_image_exists()

    def _ensure_image_exists(self):
        result = subprocess.run(
            [self.command, "image", "exists", settings.sandbox_image],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Sandbox image '{settings.sandbox_image}' not found for Podman. "
                "Run: cd backend/sandbox && podman build -t cad-agent-sandbox:latest ."
            )

    def run(self, input_dir: Path, output_dir: Path, timeout_s: int) -> tuple[int, str, str]:
        cmd = [
            self.command, "run", "--rm",
            "--network", "none",
            "--memory", settings.sandbox_memory_limit,
            "--cpus", "1",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--pids-limit", "128",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=64m,noexec,nosuid",
            "-v", f"{input_dir.as_posix()}:/sandbox/input:ro",
            "-v", f"{output_dir.as_posix()}:/sandbox/output:rw",
            settings.sandbox_image,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        return result.returncode, result.stdout, result.stderr


class LocalRuntime:
    """DEV-ONLY fallback: run the generated code in a host subprocess (no container).

    For machines without Docker/Podman (e.g. local Windows dev). The same
    executor_entry.py restricted-exec (import whitelist, stripped builtins) still
    applies, but there is NO OS-level isolation — never enable this on a deployment
    that serves untrusted users."""

    def __init__(self):
        import importlib.util

        if importlib.util.find_spec("cadquery") is None:
            raise RuntimeError(
                "SANDBOX_RUNTIME=local requires 'cadquery' importable in the backend "
                "environment. Run: pip install cadquery"
            )
        self.entry = Path(__file__).resolve().parents[2] / "sandbox" / "executor_entry.py"
        if not self.entry.exists():
            raise RuntimeError(f"Sandbox entry script not found: {self.entry}")
        logger.warning(
            "SANDBOX_RUNTIME=local: generated code runs on the host WITHOUT container "
            "isolation. Use only for local development."
        )

    def run(self, input_dir: Path, output_dir: Path, timeout_s: int) -> tuple[int, str, str]:
        env = os.environ.copy()
        # as_posix() keeps the paths valid inside Python string literals when
        # executor_entry remaps '/sandbox/output' occurrences in generated code.
        env["CAD_SANDBOX_INPUT"] = input_dir.as_posix()
        env["CAD_SANDBOX_OUTPUT"] = output_dir.as_posix()
        # Force UTF-8 in the child so its own file reads/prints don't hit the host
        # locale (e.g. cp936 on zh-Hans Windows) — CAD prompts are Chinese.
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # Decode captured stdout/stderr as UTF-8 with errors='replace': OCP/VTK can
        # emit non-locale bytes on stderr, and a strict cp936 decode here would raise
        # UnicodeDecodeError in THIS process (uncaught, → 500) instead of the child.
        result = subprocess.run(
            [sys.executable, str(self.entry)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env=env,
            cwd=str(output_dir),
        )
        return result.returncode, result.stdout, result.stderr


class CadQueryExecutor:
    def __init__(self):
        self._client = None
        self._semaphore: asyncio.Semaphore | None = None

    @property
    def runtime(self) -> str:
        return settings.sandbox_runtime.strip().lower()

    @property
    def client(self):
        if self._client is None:
            if self.runtime == "podman":
                self._client = PodmanRuntime()
            elif self.runtime == "local":
                self._client = LocalRuntime()
            elif self.runtime == "docker":
                self._client = docker.from_env()
                try:
                    self._client.images.get(settings.sandbox_image)
                except docker.errors.ImageNotFound:
                    raise RuntimeError(
                        f"Sandbox image '{settings.sandbox_image}' not found. "
                        "Run: cd backend/sandbox && docker build -t cad-agent-sandbox:latest ."
                    )
            else:
                raise RuntimeError(
                    f"Unsupported SANDBOX_RUNTIME '{settings.sandbox_runtime}'. "
                    "Use 'docker', 'podman', or 'local' (dev only)."
                )
        return self._client

    def _get_semaphore(self) -> asyncio.Semaphore:
        # Lazily created on the running loop. Caps simultaneous container spawns so
        # N concurrent testers can't exhaust host CPU/RAM (each container = 1 CPU + 512MB).
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(settings.sandbox_max_concurrent)
        return self._semaphore

    async def execute(
        self,
        code: str,
        mode: str = "3d",
        extra_files: dict[str, Path] | None = None,
    ) -> SandboxResult:
        async with self._get_semaphore():
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._execute_sync, code, mode, extra_files
            )

    def _execute_sync(
        self,
        code: str,
        mode: str = "3d",
        extra_files: dict[str, Path] | None = None,
    ) -> SandboxResult:
        work_dir = Path(tempfile.mkdtemp(prefix="cad_"))
        input_dir = work_dir / "input"
        output_dir = work_dir / "output"
        input_dir.mkdir()
        output_dir.mkdir()

        # The 'local' runtime executes code on the host with NO container isolation,
        # so the AST escape-filter is the ONLY defense here. Gate every local-mode
        # execution with it (defense in depth) — the docker/podman paths rely on the
        # container and are gated upstream by the orchestrator on the generate path.
        # 'analysis' runs a trusted in-repo script (imports OCP/json outside the
        # whitelist), so it is exempt.
        if self.runtime == "local" and mode != "analysis":
            from app.sandbox.code_filter import validate_code
            is_valid, filter_error = validate_code(code)
            if not is_valid:
                return SandboxResult(
                    success=False, files={},
                    error_type="ValidationError",
                    error_message=f"代码未通过安全校验（本地运行模式）: {filter_error}",
                    traceback=None,
                    execution_time_ms=0, work_dir=work_dir,
                )

        # Write input code and execution mode
        (input_dir / "input.py").write_text(code,encoding="utf-8")
        (input_dir / "mode.txt").write_text(mode,encoding="utf-8")

        # Copy extra files into the sandbox input directory
        if extra_files:
            import shutil
            for name, src_path in extra_files.items():
                if src_path.exists():
                    shutil.copy2(str(src_path), str(input_dir / name))

        container = None
        start_time = time.time()

        # Acquire the Docker client + spawn the container, returning a clean
        # SandboxResult on any infrastructure failure (daemon down, image missing,
        # API error) instead of bubbling an unhandled exception up the pipeline.
        try:
            client = self.client
        except Exception as e:
            return SandboxResult(
                success=False, files={},
                error_type="SandboxUnavailable",
                error_message=f"沙箱不可用: {e}",
                traceback=None,
                execution_time_ms=0, work_dir=work_dir,
            )

        if self.runtime in ("podman", "local"):
            try:
                returncode, stdout, stderr = client.run(input_dir, output_dir, settings.sandbox_timeout_s)
            except subprocess.TimeoutExpired:
                elapsed = int((time.time() - start_time) * 1000)
                return SandboxResult(
                    success=False, files={},
                    error_type="TimeoutError",
                    error_message=f"Execution timed out after {settings.sandbox_timeout_s}s",
                    traceback=None, execution_time_ms=elapsed, work_dir=work_dir,
                )
            if returncode != 0:
                # executor_entry exits 1 on a caught code error but still writes a
                # structured result.json — prefer that over raw stderr when present.
                if (output_dir / "result.json").exists():
                    return self._read_result(output_dir, work_dir, start_time)
                elapsed = int((time.time() - start_time) * 1000)
                return SandboxResult(
                    success=False, files={},
                    error_type="RuntimeError",
                    error_message=(stderr or stdout or f"Sandbox process exited with {returncode}").strip(),
                    traceback=None, execution_time_ms=elapsed, work_dir=work_dir,
                )
            return self._read_result(output_dir, work_dir, start_time)

        try:
            try:
                container = client.containers.run(
                    image=settings.sandbox_image,
                    detach=True,
                    volumes={
                        str(input_dir): {"bind": "/sandbox/input", "mode": "ro"},
                        str(output_dir): {"bind": "/sandbox/output", "mode": "rw"},
                    },
                    network_mode="none",
                    mem_limit=settings.sandbox_memory_limit,
                    nano_cpus=1_000_000_000,
                    # === Hardening: the container runs untrusted LLM-generated code ===
                    # rootfs is read-only; the only writable paths are the rw output mount
                    # and an in-memory /tmp (CadQuery/OCP scratch space). This blocks an
                    # escaped payload from persisting to or tampering with the image.
                    read_only=True,
                    tmpfs={"/tmp": "rw,size=64m,noexec,nosuid"},
                    # drop every Linux capability — sandbox needs none of them
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges"],
                    # bound process count (fork-bomb) and open files / file size
                    pids_limit=128,
                    ulimits=[
                        docker.types.Ulimit(name="nofile", soft=256, hard=512),
                        docker.types.Ulimit(name="fsize", soft=64 * 1024 * 1024, hard=64 * 1024 * 1024),
                    ],
                )
            except docker.errors.DockerException as e:
                # Failed to even start the container (API error, OOM, bad config).
                return SandboxResult(
                    success=False, files={},
                    error_type="DockerError",
                    error_message=f"无法启动沙箱容器: {e}",
                    traceback=None,
                    execution_time_ms=int((time.time() - start_time) * 1000),
                    work_dir=work_dir,
                )

            # Wait for completion
            try:
                container.wait(timeout=settings.sandbox_timeout_s)
            except Exception:
                try:
                    container.kill()
                except Exception:
                    pass
                elapsed = int((time.time() - start_time) * 1000)
                return SandboxResult(
                    success=False,
                    files={},
                    error_type="TimeoutError",
                    error_message=f"Execution timed out after {settings.sandbox_timeout_s}s",
                    traceback=None,
                    execution_time_ms=elapsed,
                    work_dir=work_dir,
                )
        finally:
            if container:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

        return self._read_result(output_dir, work_dir, start_time)

    def _read_result(self, output_dir: Path, work_dir: Path, start_time: float) -> SandboxResult:
        elapsed = int((time.time() - start_time) * 1000)

        # Read result
        result_path = output_dir / "result.json"
        if not result_path.exists():
            return SandboxResult(
                success=False,
                files={},
                error_type="RuntimeError",
                error_message="No result.json produced by sandbox",
                traceback=None,
                execution_time_ms=elapsed,
                work_dir=work_dir,
            )

        # result.json can be truncated/corrupt when the process was killed mid-write
        # (OOM/rc=137, external kill, disk full). Never let a parse error escape — it
        # would propagate out of execute() as an unhandled 500 AND leak this work_dir
        # (the caller only rmtree's via the returned SandboxResult). Fall back to a
        # clean structured error that feeds the normal retry path.
        try:
            result_data = json.loads(result_path.read_text(encoding="utf-8"))
            status = result_data["status"]
        except (json.JSONDecodeError, KeyError, ValueError, OSError) as e:
            return SandboxResult(
                success=False,
                files={},
                error_type="RuntimeError",
                error_message=f"Corrupt or truncated result.json from sandbox: {e}",
                traceback=None,
                execution_time_ms=elapsed,
                work_dir=work_dir,
            )

        if status == "success":
            files = {}
            for name, container_path in result_data.get("files", {}).items():
                # Map container path to host path
                local_path = output_dir / Path(container_path).name
                if local_path.exists():
                    files[name] = local_path
            return SandboxResult(
                success=True,
                files=files,
                error_type=None,
                error_message=None,
                traceback=None,
                execution_time_ms=elapsed,
                work_dir=work_dir,
            )
        else:
            return SandboxResult(
                success=False,
                files={},
                error_type=result_data.get("error_type"),
                error_message=result_data.get("error_message"),
                traceback=result_data.get("traceback"),
                execution_time_ms=elapsed,
                work_dir=work_dir,
            )
