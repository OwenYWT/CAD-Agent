"""A wait with no elapsed-time limit, bounded history, and explicit cancellation."""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError


@workflow.defn(name='ModelJobWorkflow')
class ModelJobWorkflow:
    async def control(self, name: str, request: dict) -> dict:
        # These activities only execute a DB transaction, never call a model.
        return await workflow.execute_activity('model_jobs.' + name, request,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3), result_type=dict)

    @workflow.run
    async def run(self, request: dict) -> dict:
        reference = {'job_id':request['job_id'],'payload':{
            key:request['payload'][key] for key in ('tenant_id','principal_id','workflow_run_id')}}
        try:
            state = await self.control('submit',request)
            if state['status'] == 'cancelled':
                raise asyncio.CancelledError
            for _ in range(500):
                state = await self.control('read',reference)
                if state['status'] == 'succeeded':
                    return state['result']
                if state['status'] == 'failed':
                    error = state['error']
                    raise ApplicationError(error['message'], *error.get('details',[]),
                        type=error['code'], non_retryable=True)
                if state['status'] == 'cancelled':
                    raise asyncio.CancelledError
                await workflow.sleep(timedelta(seconds=2))
            # Rollover only the polling history; the same job and provider call
            # continue. This is not a generation retry or a waiting deadline.
            workflow.continue_as_new(request)
        except asyncio.CancelledError:
            await asyncio.shield(self.control('cancel',reference))
            raise
