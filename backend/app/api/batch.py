"""批量生成 + 异步任务 API"""
import asyncio
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.auth import verify_api_key, rate_limiter
from app.models.schemas import GenerateResponse, ManufacturingProfile
from app.storage.file_ownership import claim_request_owner
from app.workflows.local import get_local_workflow_manager

router = APIRouter(prefix="/api", tags=["batch"])


# ── Request/Response Models ────────────────────────────────

class BatchItem(BaseModel):
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


class AsyncGenerateRequest(BaseModel):
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
            from app.api.websocket import _get_orchestrator
            orchestrator = _get_orchestrator()
            kwargs = {}
            if item.manufacturing_profile is not None:
                kwargs["manufacturing_profile"] = item.manufacturing_profile
            return await orchestrator.generate(
                prompt=item.prompt,
                output_formats=item.output_formats,
                **kwargs,
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
            await claim_request_owner(r.request_id, api_key)
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
    """异步生成 — 状态持久化，计算在当前 API 进程中后台执行。"""
    await rate_limiter.check(request, api_key)

    from app.api.websocket import _get_orchestrator

    orchestrator = _get_orchestrator()

    async def runner(on_progress):
        kwargs = {}
        if req.manufacturing_profile is not None:
            kwargs["manufacturing_profile"] = req.manufacturing_profile
        result = await orchestrator.generate(
            prompt=req.prompt,
            output_formats=req.output_formats,
            on_step=on_progress,
            **kwargs,
        )
        await claim_request_owner(result.request_id, api_key)
        return result

    manager = get_local_workflow_manager()
    task_id = await manager.submit(
        kind="generate",
        owner=api_key,
        request=req.model_dump(mode="json"),
        runner=runner,
    )

    return AsyncTaskStatus(task_id=task_id, status="pending")


@router.get("/tasks/{task_id}", response_model=AsyncTaskStatus)
async def get_task_status(
    task_id: str,
    api_key: str | None = Depends(verify_api_key),
):
    """查询异步任务状态"""
    task = await get_local_workflow_manager().get(task_id, owner=api_key)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    status = {
        "PENDING": "pending",
        "RUNNING": "running",
        "INTERRUPTED": "running",
        "RECONCILING": "running",
        "CANCEL_REQUESTED": "running",
        "COMPLETED": "completed",
        "FAILED": "failed",
        "CANCELLED": "failed",
    }[task["state"]]
    result = (
        GenerateResponse.model_validate(task["result"])
        if task["result"] is not None
        else None
    )
    return AsyncTaskStatus(
        task_id=task_id,
        status=status,
        result=result,
    )
