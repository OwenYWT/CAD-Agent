"""Own the durable provider runner for exactly the lifetime of the V2 worker."""
import asyncio

from temporalio.worker import Worker
from app.services.model_jobs import run_model_jobs


class ModelJobWorker(Worker):
    def __init__(self, *args, model_operations: dict, **kwargs):
        super().__init__(*args, **kwargs)
        self.model_operations = model_operations

    async def __aexit__(self, exc_type, *args):
        # The base context manager cancels run() after Temporal shutdown.
        # Our run() still has provider leases/connections to release, so join
        # that cleanup instead of cancelling it and leaking cancellation into
        # the caller after the context has already returned.
        await self.shutdown()
        await self._async_context_run_task
        if self._async_context_run_exception is not None:
            raise self._async_context_run_exception

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
