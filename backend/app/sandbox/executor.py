import asyncio
import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import docker

from app.config import settings


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
    def __init__(self, image_ref: str, sandbox_command: str | None = None):
        configured = (sandbox_command or settings.sandbox_command or "").strip()
        # SANDBOX_RUNTIME is authoritative. A stale template value such as
        # SANDBOX_COMMAND=docker must not make the Podman adapter invoke Docker.
        self.command = "podman" if configured in {"", "docker", "podman"} else configured
        self.image_ref = image_ref
        self._ensure_image_exists()

    def _ensure_image_exists(self):
        result = subprocess.run(
            [self.command, "image", "exists", self.image_ref],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Sandbox image '{self.image_ref}' not found for Podman. "
                "Run: podman build -f backend/sandbox/Dockerfile "
                "-t cad-agent-sandbox:dev ."
            )

    def run(
        self,
        input_dir: Path,
        output_dir: Path,
        timeout_s: int,
        resource_limits=None,
    ) -> tuple[int, str, str]:
        if resource_limits is None:
            memory = settings.sandbox_memory_limit
            cpus = "1"
            pids = "128"
            tmpfs_size = "64m"
            output_bytes = 64 * 1024 * 1024
        else:
            memory = str(resource_limits.memory_bytes)
            cpus = f"{resource_limits.cpu_millis / 1000:g}"
            pids = str(resource_limits.pids)
            tmpfs_bytes = min(
                768 * 1024 * 1024,
                max(64 * 1024 * 1024, resource_limits.memory_bytes // 2),
            )
            tmpfs_size = f"{tmpfs_bytes // (1024 * 1024)}m"
            output_bytes = resource_limits.output_bytes
        cmd = [
            self.command, "run", "--rm",
            "--network", "none",
            "--memory", memory,
            "--cpus", cpus,
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--pids-limit", pids,
            "--ulimit", "nofile=256:512",
            "--ulimit", f"fsize={output_bytes}:{output_bytes}",
            "--read-only",
            "--tmpfs", f"/tmp:rw,size={tmpfs_size},noexec,nosuid",
            "-v", f"{input_dir.as_posix()}:/sandbox/input:ro",
            "-v", f"{output_dir.as_posix()}:/sandbox/output:rw",
            self.image_ref,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        return result.returncode, result.stdout, result.stderr


class CadQueryExecutor:
    def __init__(
        self,
        runtime_name: str | None = None,
        image_ref: str | None = None,
        sandbox_command: str | None = None,
    ):
        self._client = None
        self._semaphore: asyncio.Semaphore | None = None
        self._runtime_name = (runtime_name or settings.sandbox_runtime).strip().lower()
        self.image_ref = image_ref or settings.sandbox_image
        self.sandbox_command = sandbox_command

    @property
    def runtime(self) -> str:
        return self._runtime_name

    @property
    def client(self):
        if self._client is None:
            if self.runtime == "podman":
                self._client = PodmanRuntime(self.image_ref, self.sandbox_command)
            elif self.runtime == "docker":
                self._client = docker.from_env()
                try:
                    self._client.images.get(self.image_ref)
                except docker.errors.ImageNotFound:
                    raise RuntimeError(
                        f"Sandbox image '{self.image_ref}' not found. "
                        "Run: docker build -f backend/sandbox/Dockerfile "
                        "-t cad-agent-sandbox:dev ."
                    )
            else:
                raise RuntimeError(
                    f"Unsupported SANDBOX_RUNTIME '{settings.sandbox_runtime}'. Use 'docker' or 'podman'."
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
        timeout_s: int | None = None,
        task: dict | None = None,
        resource_limits=None,
    ) -> SandboxResult:
        async with self._get_semaphore():
            loop = asyncio.get_event_loop()
            if timeout_s is None and task is None and resource_limits is None:
                return await loop.run_in_executor(
                    None,
                    self._execute_sync,
                    code,
                    mode,
                    extra_files,
                )
            return await loop.run_in_executor(
                None,
                self._execute_sync,
                code,
                mode,
                extra_files,
                timeout_s,
                task,
                resource_limits,
            )

    def _execute_sync(
        self,
        code: str,
        mode: str = "3d",
        extra_files: dict[str, Path] | None = None,
        timeout_s: int | None = None,
        task: dict | None = None,
        resource_limits=None,
    ) -> SandboxResult:
        effective_timeout = timeout_s or settings.sandbox_timeout_s
        work_dir = Path(tempfile.mkdtemp(prefix="cad_"))
        input_dir = work_dir / "input"
        output_dir = work_dir / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        if self.runtime == "podman":
            output_dir.chmod(0o777)

        # Write input code and execution mode
        (input_dir / "input.py").write_text(code,encoding="utf-8")
        (input_dir / "mode.txt").write_text(mode,encoding="utf-8")
        if task is not None:
            (input_dir / "task.json").write_text(
                json.dumps(task, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

        # Copy extra files into the sandbox input directory
        if extra_files:
            import shutil
            for name, src_path in extra_files.items():
                if (
                    Path(name).name != name
                    or name.startswith(("-", "."))
                    or "/" in name
                    or "\\" in name
                ):
                    raise ValueError(f"unsafe sandbox input filename: {name}")
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

        if self.runtime == "podman":
            try:
                returncode, stdout, stderr = client.run(
                    input_dir,
                    output_dir,
                    effective_timeout,
                    resource_limits,
                )
            except subprocess.TimeoutExpired:
                elapsed = int((time.time() - start_time) * 1000)
                return SandboxResult(
                    success=False, files={},
                    error_type="TimeoutError",
                    error_message=f"Execution timed out after {effective_timeout}s",
                    traceback=None, execution_time_ms=elapsed, work_dir=work_dir,
                )
            if returncode != 0 and not (output_dir / "result.json").exists():
                elapsed = int((time.time() - start_time) * 1000)
                if returncode == 137:
                    return SandboxResult(
                        success=False,
                        files={},
                        error_type="OOMError",
                        error_message="Execution exceeded the sandbox memory limit",
                        traceback=None,
                        execution_time_ms=elapsed,
                        work_dir=work_dir,
                    )
                return SandboxResult(
                    success=False, files={},
                    error_type="RuntimeError",
                    error_message=(stderr or stdout or f"Podman exited with {returncode}").strip(),
                    traceback=None, execution_time_ms=elapsed, work_dir=work_dir,
                )
            return self._read_result(output_dir, work_dir, start_time)

        try:
            try:
                memory_limit = (
                    resource_limits.memory_bytes
                    if resource_limits is not None
                    else settings.sandbox_memory_limit
                )
                cpu_millis = (
                    resource_limits.cpu_millis
                    if resource_limits is not None
                    else 1000
                )
                pids_limit = (
                    resource_limits.pids
                    if resource_limits is not None
                    else 128
                )
                output_bytes = (
                    resource_limits.output_bytes
                    if resource_limits is not None
                    else 64 * 1024 * 1024
                )
                tmpfs_bytes = (
                    min(
                        768 * 1024 * 1024,
                        max(64 * 1024 * 1024, resource_limits.memory_bytes // 2),
                    )
                    if resource_limits is not None
                    else 64 * 1024 * 1024
                )
                container = client.containers.run(
                    image=self.image_ref,
                    detach=True,
                    volumes={
                        str(input_dir): {"bind": "/sandbox/input", "mode": "ro"},
                        str(output_dir): {"bind": "/sandbox/output", "mode": "rw"},
                    },
                    network_mode="none",
                    mem_limit=memory_limit,
                    nano_cpus=cpu_millis * 1_000_000,
                    # === Hardening: the container runs untrusted LLM-generated code ===
                    # rootfs is read-only; the only writable paths are the rw output mount
                    # and an in-memory /tmp (CadQuery/OCP scratch space). This blocks an
                    # escaped payload from persisting to or tampering with the image.
                    read_only=True,
                    tmpfs={"/tmp": f"rw,size={tmpfs_bytes},noexec,nosuid"},
                    # drop every Linux capability — sandbox needs none of them
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges"],
                    # bound process count (fork-bomb) and open files / file size
                    pids_limit=pids_limit,
                    ulimits=[
                        docker.types.Ulimit(name="nofile", soft=256, hard=512),
                        docker.types.Ulimit(
                            name="fsize",
                            soft=output_bytes,
                            hard=output_bytes,
                        ),
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
                container.wait(timeout=effective_timeout)
                state = getattr(container, "attrs", {}).get("State", {})
                if isinstance(state, dict) and state.get("OOMKilled") is True:
                    return SandboxResult(
                        success=False,
                        files={},
                        error_type="OOMError",
                        error_message="Execution exceeded the sandbox memory limit",
                        traceback=None,
                        execution_time_ms=int((time.time() - start_time) * 1000),
                        work_dir=work_dir,
                    )
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
                    error_message=f"Execution timed out after {effective_timeout}s",
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

        result_data = json.loads(result_path.read_text())

        if result_data["status"] == "success":
            files = {}
            for name, container_path in result_data["files"].items():
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
