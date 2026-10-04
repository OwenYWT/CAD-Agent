"""Executor resilience: clean failure on Docker problems + concurrency cap (#24).
Hermetic — no real Docker daemon."""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.sandbox.executor import CadQueryExecutor


def test_private_inputs_are_readable_only_in_the_read_only_staging_mount(tmp_path):
    source = tmp_path / 'private.step'
    source.write_bytes(b'private artifact')
    source.chmod(0o600)
    executor = CadQueryExecutor(runtime_name='docker')
    container = MagicMock()
    container.wait.return_value = {'StatusCode': 0}

    def run(*, volumes, **kwargs):
        inputs = next(Path(path) for path, spec in volumes.items() if spec['bind'] == '/sandbox/input')
        outputs = next(Path(path) for path, spec in volumes.items() if spec['bind'] == '/sandbox/output')
        assert volumes[str(inputs)]['mode'] == 'ro'
        assert inputs.parent.stat().st_mode & 0o777 == 0o700
        assert inputs.stat().st_mode & 0o777 == 0o755
        assert {path.name for path in inputs.iterdir()} == {'input.py', 'mode.txt', 'task.json', 'model.step'}
        for path in inputs.iterdir():
            assert path.stat().st_mode & 0o777 == 0o444
        assert (inputs / 'model.step').read_bytes() == source.read_bytes()
        outputs.joinpath('result.json').write_text(json.dumps({'status': 'success', 'files': {}}))
        return container

    executor._client = MagicMock()
    executor._client.containers.run = run
    old_umask = os.umask(0o077)
    try:
        result = executor._execute_sync('code', extra_files={'model.step': source}, task={'operation': 'inspect'})
    finally:
        os.umask(old_umask)
    assert result.success
    assert source.stat().st_mode & 0o777 == 0o600
    assert source.read_bytes() == b'private artifact'


@pytest.mark.parametrize('bounded', [True, False], ids=['bounded', 'unbounded'])
def test_wrapped_docker_read_timeout_respects_execution_budget(bounded):
    from requests.exceptions import ConnectionError
    from urllib3.exceptions import ReadTimeoutError
    from app.execution.contracts import ResourceLimits

    executor = CadQueryExecutor(runtime_name='docker')
    container = MagicMock()
    container.wait.side_effect = ConnectionError(ReadTimeoutError(None, '/containers/test/wait', 'read deadline'))
    executor._client = MagicMock()
    executor._client.containers.run.return_value = container
    result = executor._execute_sync('code', resource_limits=ResourceLimits(timeout_seconds=1 if bounded else None), timeout_s=1)
    assert result.error_type == ('TimeoutError' if bounded else 'DockerError')
    container.wait.assert_called_once_with(timeout=1 if bounded else None)
    container.kill.assert_called_once()
    container.remove.assert_called_once_with(force=True)


def test_bounded_daemon_connection_failure_is_not_an_execution_timeout():
    from requests.exceptions import ConnectionError

    executor = CadQueryExecutor(runtime_name='docker')
    container = MagicMock()
    container.wait.side_effect = ConnectionError('daemon disconnected')
    executor._client = MagicMock()
    executor._client.containers.run.return_value = container
    result = executor._execute_sync('code', timeout_s=1)
    assert result.error_type == 'DockerError'
    assert 'daemon disconnected' in result.error_message
    container.remove.assert_called_once_with(force=True)


def test_unbounded_wait_and_daemon_failure_are_not_a_fake_timeout():
    from unittest.mock import MagicMock
    from app.execution.contracts import ResourceLimits
    ex=CadQueryExecutor(runtime_name='docker')
    container=MagicMock()
    container.wait.side_effect=RuntimeError('daemon connection lost')
    ex._client=MagicMock()
    ex._client.containers.run.return_value=container
    result=ex._execute_sync('code', resource_limits=ResourceLimits(timeout_seconds=None))
    container.wait.assert_called_once_with(timeout=None)
    assert result.error_type=='DockerError'
    assert 'daemon connection lost' in result.error_message
    container.remove.assert_called_once_with(force=True)


@pytest.mark.asyncio
async def test_execute_returns_clean_result_when_docker_unavailable(monkeypatch):
    """client property raising (daemon down / image missing) → SandboxUnavailable result,
    not an unhandled exception bubbling up the pipeline."""
    ex = CadQueryExecutor()

    # make the client property raise as if Docker is down
    def boom(self):
        raise RuntimeError("Cannot connect to the Docker daemon")
    monkeypatch.setattr(type(ex), "client", property(boom))

    result = await ex.execute("result = 1\nshow_object(result)")
    assert result.success is False
    assert result.error_type == "SandboxUnavailable"
    assert "沙箱不可用" in result.error_message
    assert result.work_dir.exists()  # work dir created + returned for cleanup


@pytest.mark.asyncio
async def test_concurrency_cap_limits_parallel_executions(monkeypatch):
    """The semaphore caps simultaneous _execute_sync calls at sandbox_max_concurrent."""
    monkeypatch.setattr(settings, "sandbox_max_concurrent", 2)
    ex = CadQueryExecutor()

    active = 0
    peak = 0

    def fake_sync(code, mode, extra_files):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        # busy a moment so overlap is observable
        import time as _t
        _t.sleep(0.05)
        active -= 1
        from app.sandbox.executor import SandboxResult
        from pathlib import Path
        import tempfile
        return SandboxResult(
            success=True, files={}, error_type=None, error_message=None,
            traceback=None, execution_time_ms=1, work_dir=Path(tempfile.mkdtemp()),
        )

    monkeypatch.setattr(ex, "_execute_sync", fake_sync)

    # launch 6 concurrently; cap is 2
    await asyncio.gather(*[ex.execute("c") for _ in range(6)])
    assert peak <= 2, f"peak concurrency {peak} exceeded cap of 2"
