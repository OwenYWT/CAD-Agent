import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.websocket import _get_orchestrator
from app.api.auth import verify_api_key, rate_limiter
from app.models.schemas import ExecuteRequest, GenerateResponse
from app.storage.file_ownership import claim_request_owner

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/execute", response_model=GenerateResponse)
async def execute(req: ExecuteRequest, request: Request, api_key: str | None = Depends(verify_api_key)):
    """直接执行 CadQuery 代码 (参数修改用, 无 LLM)"""
    await rate_limiter.check(request, api_key)
    try:
        orchestrator = _get_orchestrator()
        response = await orchestrator.execute_code(req.code, req.output_formats)
        await claim_request_owner(response.request_id, api_key)
        if not response.success:
            return JSONResponse(
                status_code=500,
                content=response.model_dump(),
            )
        return response
    except Exception as e:
        logger.exception("Execute failed")
        request_id = str(uuid.uuid4())
        return JSONResponse(
            status_code=500,
            content=GenerateResponse(
                request_id=request_id,
                success=False,
                error={"type": type(e).__name__, "message": str(e)},
            ).model_dump(),
        )
