"""批量生成 + 异步任务 API"""
import asyncio
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.auth import verify_api_key, rate_limiter
from app.config import settings
from app.models.schemas import (
    DurableRequestIdentity,
    GenerateResponse,
    ManufacturingProfile,
)
from app.principal_context import current_principal
from app.services.durable_submission import (
    get_compatibility_response,
    submit_durable_workflow,
    wait_for_compatibility_response,
)

router = APIRouter(prefix="/api", tags=["batch"])


# ── Request/Response Models ────────────────────────────────

class BatchItem(DurableRequestIdentity):
    prompt: str
    manufacturing_profile: ManufacturingProfile | None = None
    output_formats: list[str] = ["step", "stl"]


class BatchRequest(BaseModel):
    items: list[BatchItem]  # max 20 items enforced in endpoint
    max_concurrent: int = 3


class BatchResponse(BaseModel):
    request_id: str
    results: list[GenerateResponse]
    total_time_ms: int


class AsyncGenerateRequest(DurableRequestIdentity):
    prompt: str
    manufacturing_profile: ManufacturingProfile | None = None
    output_formats: list[str] = ["step", "stl"]


class AsyncTaskStatus(BaseModel):
    task_id: str
    status: str  # "pending" | "running" | "completed" | "failed"
    result: GenerateResponse | None = None


# ── Batch Generate ─────────────────────────────────────────

@router.post("/batch/generate", response_model=BatchResponse)
async def batch_generate(
    req: BatchRequest,
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    """并发执行多个生成任务"""
    if len(req.items) > 20:
        raise HTTPException(status_code=400, detail="Batch size cannot exceed 20 items")

    await rate_limiter.check(request, api_key)

    start = time.time()
    request_id = str(uuid.uuid4())

    semaphore = asyncio.Semaphore(max(1, min(req.max_concurrent, 10)))

    async def run_one(item: BatchItem) -> GenerateResponse:
        async with semaphore:
            submission = await submit_durable_workflow(
                current_principal(),
                project_id=item.project_id,
                branch_id=item.branch_id,
                expected_base_revision_id=(
                    item.expected_base_revision_id
                ),
                idempotency_key=item.idempotency_key,
                operation="generate",
                objective=item.prompt,
                output_formats=item.output_formats,
                manufacturing_profile=item.manufacturing_profile,
            )
            return await wait_for_compatibility_response(
                current_principal(),
                submission,
                timeout_seconds=settings.generate_deadline_s,
            )

    results = await asyncio.gather(
        *[run_one(item) for item in req.items],
        return_exceptions=True,
    )

    # Convert exceptions to failed responses
    final_results = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            final_results.append(GenerateResponse(
                request_id=f"{request_id}-{i}",
                success=False,
                error={"type": type(r).__name__, "message": str(r)},
            ))
        else:
            final_results.append(r)

    total_ms = int((time.time() - start) * 1000)

    return BatchResponse(
        request_id=request_id,
        results=final_results,
        total_time_ms=total_ms,
    )


# ── Async Generate ─────────────────────────────────────────

@router.post("/generate/async", response_model=AsyncTaskStatus)
async def generate_async(
    req: AsyncGenerateRequest,
    request: Request,
    api_key: str | None = Depends(verify_api_key),
):
    """异步生成；任务由持久工作流执行，与 API 进程生命周期解耦。"""
    await rate_limiter.check(request, api_key)

    submission = await submit_durable_workflow(
        current_principal(),
        project_id=req.project_id,
        branch_id=req.branch_id,
        expected_base_revision_id=req.expected_base_revision_id,
        idempotency_key=req.idempotency_key,
        operation="generate",
        objective=req.prompt,
        output_formats=req.output_formats,
        manufacturing_profile=req.manufacturing_profile,
    )
    return AsyncTaskStatus(
        task_id=str(submission.workflow_run_id),
        status="pending",
    )


@router.get("/tasks/{task_id}", response_model=AsyncTaskStatus)
async def get_task_status(
    task_id: str,
    api_key: str | None = Depends(verify_api_key),
):
    """查询异步任务状态"""
    try:
        from uuid import UUID

        result = await get_compatibility_response(
            current_principal(),
            UUID(task_id),
        )
    except (ValueError, KeyError):
        raise HTTPException(status_code=404, detail="Task not found")
    status = {
        "pending": "pending",
        "planning": "running",
        "running": "running",
        "waiting_confirmation": "running",
        "cancelling": "running",
        "succeeded": "completed",
        "failed": "failed",
        "cancelled": "failed",
        "timed_out": "failed",
    }.get(result.task_status or "", "running")
    return AsyncTaskStatus(
        task_id=task_id,
        status=status,
        result=(
            result
            if status in {"completed", "failed"}
            or result.needs_confirmation
            else None
        ),
    )
