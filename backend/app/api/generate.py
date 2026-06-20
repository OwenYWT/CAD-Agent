import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.websocket import _get_orchestrator
from app.api.auth import verify_api_key, rate_limiter
from app.config import settings
from app.models.schemas import GenerateRequest, GenerateResponse, ModifyRequest

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest, request: Request, api_key: str | None = Depends(verify_api_key)):
    """从自然语言描述生成 CAD 模型 (同步, 有总超时上限)"""
    await rate_limiter.check(request, api_key)
    try:
        orchestrator = _get_orchestrator()
        # Hard overall deadline: planning + N×LLM + N×sandbox can otherwise run for
        # minutes. Fail fast instead of hanging the tester's request.
        response = await asyncio.wait_for(
            orchestrator.generate(req.prompt, req.output_formats),
            timeout=settings.generate_deadline_s,
        )
        if not response.success:
            return JSONResponse(
                status_code=500,
                content=response.model_dump(),
            )
        return response
    except asyncio.TimeoutError:
        logger.warning("Generate exceeded deadline of %ss", settings.generate_deadline_s)
        return JSONResponse(
            status_code=504,
            content=GenerateResponse(
                request_id=str(uuid.uuid4()),
                success=False,
                error={"type": "TimeoutError", "message": f"生成超时（>{int(settings.generate_deadline_s)}s），请简化需求后重试"},
            ).model_dump(),
        )
    except Exception as e:
        logger.exception("Generate failed")
        request_id = str(uuid.uuid4())
        return JSONResponse(
            status_code=500,
            content=GenerateResponse(
                request_id=request_id,
                success=False,
                error={"type": type(e).__name__, "message": str(e)},
            ).model_dump(),
        )


@router.post("/api/modify", response_model=GenerateResponse)
async def modify(req: ModifyRequest, request: Request, api_key: str | None = Depends(verify_api_key)):
    """基于已有代码修改 CAD 模型"""
    await rate_limiter.check(request, api_key)
    try:
        orchestrator = _get_orchestrator()
        response = await orchestrator.modify(req.code, req.prompt, req.output_formats)
        if not response.success:
            return JSONResponse(
                status_code=500,
                content=response.model_dump(),
            )
        return response
    except Exception as e:
        logger.exception("Modify failed")
        request_id = str(uuid.uuid4())
        return JSONResponse(
            status_code=500,
            content=GenerateResponse(
                request_id=request_id,
                success=False,
                error={"type": type(e).__name__, "message": str(e)},
            ).model_dump(),
        )
