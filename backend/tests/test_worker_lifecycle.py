"""Lifecycle fault injection; no SDK private context-manager state."""
import asyncio
from types import MethodType

import pytest
from app.workers.model_job_worker import ModelJobWorker


@pytest.mark.asyncio
@pytest.mark.parametrize('fail', [False, True])
async def test_context_joins_work_and_propagates_real_error(fail):
    worker = object.__new__(ModelJobWorker)
    stopped = asyncio.Event()
    cleaned = asyncio.Event()

    async def run(self):
        try:
            if fail:
                raise RuntimeError('runner unavailable')
            await stopped.wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    async def shutdown(self):
        stopped.set()

    worker.run = MethodType(run, worker)
    worker.shutdown = MethodType(shutdown, worker)
    if fail:
        with pytest.raises(RuntimeError, match='runner unavailable'):
            async with worker:
                await asyncio.Event().wait()
    else:
        async with worker:
            await asyncio.sleep(0)
    assert cleaned.is_set()
    assert worker._owned_run.done()
