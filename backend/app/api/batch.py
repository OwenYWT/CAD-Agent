"""批量生成 + 异步任务 API"""
import asyncio
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.agent.orchestrator import Orchestrator
from app.api.auth import verify_api_key, rate_limiter
from app.models.schemas import GenerateResponse
from app.storage.file_ownership import claim_request_owner

router = APIRouter(prefix="/api", tags=["batch"])

# In-memory task store (production would use Redis/DB)
_tasks: dict[str, dict] = {}


# ── Request/Response Models ────────────────────────────────

class BatchItem(BaseModel):
    prompt: str
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
            return await orchestrator.generate(
                prompt=item.prompt,
                output_formats=item.output_formats,
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
            claim_request_owner(r.request_id, api_key)
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
    """异步生成 — 立即返回 task_id，后台执行"""
    await rate_limiter.check(request, api_key)

    task_id = str(uuid.uuid4())

    _tasks[task_id] = {
        "status": "pending",
        "result": None,
        "owner": api_key,
    }

    # Launch background task
    asyncio.create_task(_run_async_generate(task_id, req, api_key))

    return AsyncTaskStatus(task_id=task_id, status="pending")


@router.get("/tasks/{task_id}", response_model=AsyncTaskStatus)
async def get_task_status(
    task_id: str,
    api_key: str | None = Depends(verify_api_key),
):
    """查询异步任务状态"""
    task = _tasks.get(task_id)
    if task is None or task.get("owner") != api_key:
        raise HTTPException(status_code=404, detail="Task not found")

    return AsyncTaskStatus(
        task_id=task_id,
        status=task["status"],
        result=task["result"],
    )


async def _run_async_generate(
    task_id: str,
    req: AsyncGenerateRequest,
    principal: str | None,
):
    """后台执行生成任务"""
    _tasks[task_id]["status"] = "running"

    try:
        orchestrator = Orchestrator()
        result = await orchestrator.generate(
            prompt=req.prompt,
            output_formats=req.output_formats,
        )
        claim_request_owner(result.request_id, principal)
        _tasks[task_id]["status"] = "completed"
        _tasks[task_id]["result"] = result
    except Exception as e:
        _tasks[task_id]["status"] = "failed"
        _tasks[task_id]["result"] = GenerateResponse(
            request_id=task_id,
            success=False,
            error={"type": type(e).__name__, "message": str(e)},
        )
