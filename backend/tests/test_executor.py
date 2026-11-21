"""
Tests for CadQueryExecutor: SandboxResult structure, mode file writing,
timeout handling, container cleanup.
Unit tests mock Docker — no actual container required.
"""
import os
import json
import asyncio
import threading
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from app.sandbox.executor import CadQueryExecutor, SandboxResult


# === SandboxResult structure ===

class TestSandboxResult:
    def test_success_result(self):
        r = SandboxResult(
            success=True,
            files={"step": Path("/tmp/result.step")},
            error_type=None,
            error_message=None,
            traceback=None,
            execution_time_ms=500,
            work_dir=Path("/tmp/cad_xxx"),
        )
        assert r.success
        assert "step" in r.files
        assert r.execution_time_ms == 500

    def test_failure_result(self):
        r = SandboxResult(
            success=False,
            files={},
            error_type="RuntimeError",
            error_message="Something broke",
            traceback="Traceback...",
            execution_time_ms=100,
            work_dir=Path("/tmp/cad_xxx"),
        )
        assert not r.success
        assert r.error_type == "RuntimeError"

    def test_timeout_result(self):
        r = SandboxResult(
            success=False,
            files={},
            error_type="TimeoutError",
            error_message="Execution timed out after 30s",
            traceback=None,
            execution_time_ms=30000,
            work_dir=Path("/tmp/cad_xxx"),
        )
        assert r.error_type == "TimeoutError"


# === Mode file writing ===

class TestModeFileWriting:
    """Verify that _execute_sync writes mode.txt correctly."""

    def test_output_mount_is_writable_by_non_root_worker(self):
        executor = CadQueryExecutor(runtime_name="docker")
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}

        def fake_run(image, detach, volumes, **kwargs):
            output_dir = next(
                Path(host_path)
                for host_path, bind_info in volumes.items()
                if bind_info["bind"] == "/sandbox/output"
            )
            assert output_dir.stat().st_mode & 0o777 == 0o777
            output_dir.joinpath("result.json").write_text(
                json.dumps({"status": "success", "files": {}})
            )
            return mock_container

        executor._client = MagicMock()
        executor._client.containers.run = fake_run

        assert executor._execute_sync("code").success

    def test_3d_mode_writes_mode_file(self):
        executor = CadQueryExecutor(runtime_name="docker")

        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}

        input_dirs_seen = []

        def fake_run(image, detach, volumes, **kwargs):
            for host_path, bind_info in volumes.items():
                if bind_info["bind"] == "/sandbox/input":
                    input_dirs_seen.append(Path(host_path))
                    for hp, bi in volumes.items():
                        if bi["bind"] == "/sandbox/output":
                            result = {"status": "success", "files": {}}
                            Path(hp, "result.json").write_text(json.dumps(result))
            return mock_container

        mock_docker = MagicMock()
        mock_docker.containers.run = fake_run
        mock_docker.images.get.return_value = True
        executor._client = mock_docker

        result = executor._execute_sync("code", mode="3d")
        assert len(input_dirs_seen) == 1
        mode_file = input_dirs_seen[0] / "mode.txt"
        assert mode_file.exists()
        assert mode_file.read_text() == "3d"

    def test_2d_mode_writes_mode_file(self):
        executor = CadQueryExecutor(runtime_name="docker")

        input_dirs_seen = []

        def fake_run(image, detach, volumes, **kwargs):
            for host_path, bind_info in volumes.items():
                if bind_info["bind"] == "/sandbox/input":
                    input_dirs_seen.append(Path(host_path))
                    for hp, bi in volumes.items():
                        if bi["bind"] == "/sandbox/output":
                            result = {"status": "success", "files": {}}
                            Path(hp, "result.json").write_text(json.dumps(result))
            return MagicMock(**{"wait.return_value": {"StatusCode": 0}})

        mock_docker = MagicMock()
        mock_docker.containers.run = fake_run
        mock_docker.images.get.return_value = True
        executor._client = mock_docker

        executor._execute_sync("code", mode="2d")
        assert len(input_dirs_seen) == 1
        mode_file = input_dirs_seen[0] / "mode.txt"
        assert mode_file.read_text() == "2d"


# === Container cleanup ===

class TestContainerCleanup:
    def test_container_removed_on_success(self):
        executor = CadQueryExecutor(runtime_name="docker")
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}

        def fake_run(image, detach, volumes, **kwargs):
            for hp, bi in volumes.items():
                if bi["bind"] == "/sandbox/output":
                    result = {"status": "success", "files": {}}
                    Path(hp, "result.json").write_text(json.dumps(result))
            return mock_container

        executor._client = MagicMock()
        executor._client.containers.run = fake_run

        executor._execute_sync("code")
        mock_container.remove.assert_called_once_with(force=True)

    def test_container_removed_on_timeout(self):
        executor = CadQueryExecutor(runtime_name="docker")
        mock_container = MagicMock()
        from requests.exceptions import ReadTimeout
        mock_container.wait.side_effect = ReadTimeout("timeout")

        executor._client = MagicMock()
        executor._client.containers.run.return_value = mock_container

        result = executor._execute_sync("code")
        assert not result.success
        assert result.error_type == "TimeoutError"
        mock_container.kill.assert_called_once()
        mock_container.remove.assert_called_once_with(force=True)


# === No result.json ===

class TestMissingResult:
    def test_utf8_result_json_is_read_without_windows_locale_decoding(self, tmp_path, monkeypatch):
        # The Windows host has neither of these POSIX open flags.
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
        executor = CadQueryExecutor()
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        result = {
            "status": "error",
            "error_type": "RuntimeError",
            "error_message": "FreeCAD 输出包含中文：尺寸更新失败",
        }
        (output_dir / "result.json").write_bytes(
            json.dumps(result, ensure_ascii=False).encode("utf-8")
        )

        parsed = executor._read_result(output_dir, tmp_path, 0.0)

        assert parsed.error_message == result["error_message"]

    def test_missing_result_json(self):
        executor = CadQueryExecutor(runtime_name="docker")
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}

        executor._client = MagicMock()
        executor._client.containers.run.return_value = mock_container

        result = executor._execute_sync("code")
        assert not result.success
        assert result.error_type == "SandboxProtocolError"
        assert result.error_code == "sandbox_protocol_error"
        assert "No result.json" in result.error_message


# === Podman runtime ===

class TestPodmanRuntime:
    def test_podman_subprocess_output_uses_utf8_with_replacement(self, monkeypatch):
        import app.sandbox.executor as executor_module

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return MagicMock(returncode=0, stdout="", stderr="")

        class FakePopen:
            returncode = 0

            def __init__(self, cmd, **kwargs):
                calls.append((cmd, kwargs))

            def communicate(self, timeout=None):
                return "", ""

        monkeypatch.setattr(executor_module.subprocess, "run", fake_run)
        monkeypatch.setattr(executor_module.subprocess, "Popen", FakePopen)

        runtime = executor_module.PodmanRuntime("cad-agent-sandbox:test")
        runtime.run(Path("input"), Path("output"), timeout_s=1)
        runtime._force_remove("cad-agent-test")

        process_options = [kwargs for _, kwargs in calls]
        assert len(process_options) == 3
        assert all(options.get("encoding") == "utf-8" for options in process_options)
        assert all(options.get("errors") == "replace" for options in process_options)

    @pytest.mark.asyncio
    async def test_cancel_waits_until_physical_podman_execution_is_stopped(self):
        started = threading.Event()
        physically_stopped = threading.Event()

        class CancellablePodman:
            def run(
                self,
                input_dir,
                output_dir,
                timeout_s,
                resource_limits=None,
                cancel_event=None,
            ):
                started.set()
                cancel_event.wait(timeout=2)
                physically_stopped.set()
                return 130, "", "cancelled"

            def cancel(self, cancel_event):
                cancel_event.set()

        executor = CadQueryExecutor(runtime_name="podman")
        executor._client = CancellablePodman()
        task = asyncio.create_task(executor.execute("result = None"))
        assert await asyncio.to_thread(started.wait, 1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert physically_stopped.is_set()

    def test_podman_runtime_writes_mode_and_invokes_podman(self, monkeypatch):
        from app.config import settings
        import app.sandbox.executor as executor_module

        original_runtime = settings.sandbox_runtime
        settings.sandbox_runtime = "podman"
        calls = []
        input_dirs_seen = []

        def fake_run(cmd, capture_output=True, text=True, timeout=None, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ["podman", "image", "exists"]:
                return MagicMock(returncode=0, stdout="", stderr="")

        class FakePopen:
            def __init__(self, cmd, **_kwargs):
                calls.append(cmd)
                output_mount = next(arg for arg in cmd if arg.endswith(":/sandbox/output:rw"))
                input_mount = next(arg for arg in cmd if arg.endswith(":/sandbox/input:ro"))
                output_dir = Path(output_mount.removesuffix(":/sandbox/output:rw"))
                input_dir = Path(input_mount.removesuffix(":/sandbox/input:ro"))
                input_dirs_seen.append(input_dir)
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "result.json").write_text(
                    json.dumps({"status": "success", "files": {}})
                )
                self.returncode = 0

            def communicate(self, timeout=None):
                return "ok", ""

        monkeypatch.setattr(executor_module.subprocess, "run", fake_run)
        monkeypatch.setattr(executor_module.subprocess, "Popen", FakePopen)

        try:
            result = CadQueryExecutor()._execute_sync("code", mode="2d")
        finally:
            settings.sandbox_runtime = original_runtime

        assert result.success
        assert input_dirs_seen[0].joinpath("mode.txt").read_text() == "2d"
        run_cmd = calls[-1]
        assert run_cmd[:3] == ["podman", "run", "--rm"]
        assert "--network" in run_cmd
        assert "none" in run_cmd
        assert "--memory" in run_cmd
        assert settings.sandbox_image in run_cmd

    def test_podman_missing_image_message(self, monkeypatch):
        from app.config import settings
        import app.sandbox.executor as executor_module

        original_runtime = settings.sandbox_runtime
        settings.sandbox_runtime = "podman"

        def fake_run(cmd, capture_output=True, text=True, timeout=None, **kwargs):
            return MagicMock(returncode=1, stdout="", stderr="missing")

        monkeypatch.setattr(executor_module.subprocess, "run", fake_run)

        try:
            with pytest.raises(RuntimeError, match="podman build"):
                _ = CadQueryExecutor().client
        finally:
            settings.sandbox_runtime = original_runtime

    def test_podman_exit_137_is_reported_as_oom(self, monkeypatch):
        from app.config import settings
        import app.sandbox.executor as executor_module

        original_runtime = settings.sandbox_runtime
        settings.sandbox_runtime = "podman"

        def fake_run(cmd, capture_output=True, text=True, timeout=None, **kwargs):
            if cmd[:3] == ["podman", "image", "exists"]:
                return MagicMock(returncode=0, stdout="", stderr="")

        class FakePopen:
            returncode = 137

            def __init__(self, cmd, **_kwargs):
                pass

            def communicate(self, timeout=None):
                return "", "Killed"

        monkeypatch.setattr(executor_module.subprocess, "run", fake_run)
        monkeypatch.setattr(executor_module.subprocess, "Popen", FakePopen)
        try:
            result = CadQueryExecutor()._execute_sync("code")
        finally:
            settings.sandbox_runtime = original_runtime

        assert result.success is False
        assert result.error_type == "OOMError"
