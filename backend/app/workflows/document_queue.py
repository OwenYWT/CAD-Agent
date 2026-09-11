"""Deterministic queue waiting shared by the V1 and V2 modeling workflows."""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError


async def wait_for_document(workflow_instance, request: dict):
    workflow_instance._phase = "document_queue"
    for attempt in range(900):
        result = await workflow_instance._activity("document.acquire", request, suffix=f"document-acquire-{attempt}")
        if result["status"] == "acquired":
            return
        if result["status"] != "waiting":
            raise ApplicationError("文档操作已失效，请读取当前版本后重新提交",
                type=result.get("error_code", "document_operation_rejected"), non_retryable=True)
        try:
            await workflow.wait_condition(lambda: workflow_instance._cancel_reason is not None,
                                          timeout=timedelta(seconds=2))
            raise asyncio.CancelledError
        except asyncio.TimeoutError:
            pass
    raise ApplicationError("等待文档操作超时", type="document_queue_timeout", non_retryable=True)


async def release_document(request: dict):
    await workflow.execute_activity("document.release", request,
        activity_id=f"{request['workflow_run_id']}:document-release",
        start_to_close_timeout=timedelta(seconds=60),
        retry_policy=RetryPolicy(maximum_attempts=5), result_type=dict)
