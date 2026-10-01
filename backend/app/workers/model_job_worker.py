"""Own the durable provider runner for exactly the lifetime of the V2 worker."""
import asyncio

from temporalio.worker import Worker
from app.services.model_jobs import run_model_jobs


class ModelJobWorker(Worker):
    def __init__(self, *args, model_operations: dict, **kwargs):
        super().__init__(*args, **kwargs)
        self.model_operations = model_operations

    async def __aenter__(self):
        self._owner_task = asyncio.current_task()
        self._owned_run = asyncio.create_task(self.run())
        self._run_failure = None

        def report_failure(task):
            if not task.cancelled() and task.exception() is not None:
                self._run_failure = task.exception()
                self._owner_task.cancel()

        self._failure_callback = report_failure
        self._owned_run.add_done_callback(report_failure)
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self._owned_run.remove_done_callback(self._failure_callback)
        await self.shutdown()
        await self._owned_run
        if self._run_failure is not None:
            raise self._run_failure
        return False

    async def run(self):
        runner = asyncio.create_task(run_model_jobs(self.model_operations))
        temporal = asyncio.create_task(super().run())
        try:
            done, _ = await asyncio.wait({runner,temporal},return_when=asyncio.FIRST_COMPLETED)
            if runner in done:
                await self.shutdown()
                await runner
            await temporal
        finally:
            runner.cancel()
            await asyncio.gather(runner,return_exceptions=True)
            if not temporal.done():
                await self.shutdown()
                await temporal
