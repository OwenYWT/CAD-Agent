"""Executor resilience: clean failure on Docker problems + concurrency cap (#24).
Hermetic — no real Docker daemon."""
import asyncio

import pytest

from app.config import settings
from app.sandbox.executor import CadQueryExecutor


@pytest.mark.asyncio
async def test_execute_returns_clean_result_when_docker_unavailable(monkeypatch):
    """client property raising (daemon down / image missing) → DockerUnavailable result,
    not an unhandled exception bubbling up the pipeline."""
    ex = CadQueryExecutor()

    # make the client property raise as if Docker is down
    def boom(self):
        raise RuntimeError("Cannot connect to the Docker daemon")
    monkeypatch.setattr(type(ex), "client", property(boom))

    result = await ex.execute("result = 1\nshow_object(result)")
    assert result.success is False
    assert result.error_type == "DockerUnavailable"
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
