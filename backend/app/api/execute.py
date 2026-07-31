import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.websocket import _get_orchestrator
from app.api.auth import verify_api_key, rate_limiter
from app.config import settings
from app.models.schemas import ExecuteRequest, GenerateResponse
from app.principal_context import current_principal
from app.repositories.revisions import StaleBaseRevision
from app.services.durable_submission import (
    submit_durable_workflow,
    wait_for_compatibility_response,
)
from app.services.run_state import IdempotencyConflict
from app.storage.file_ownership import claim_request_owner

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/execute", response_model=GenerateResponse)
async def execute(req: ExecuteRequest, request: Request, api_key: str | None = Depends(verify_api_key)):
    """直接执行 CadQuery 代码 (参数修改用, 无 LLM)"""
    await rate_limiter.check(request, api_key)
    try:
        if settings.durable_api_cutover_enabled:
            submission = await submit_durable_workflow(
                current_principal(),
                project_id=req.project_id,
                branch_id=req.branch_id,
                expected_base_revision_id=req.expected_base_revision_id,
                idempotency_key=req.idempotency_key,
                operation="execute",
                objective="执行用户提交的 MCAD 代码",
                output_formats=req.output_formats,
                code=req.code,
            )
            response = await wait_for_compatibility_response(
                current_principal(),
                submission,
                timeout_seconds=settings.generate_deadline_s,
            )
            if not response.success:
                status_code = (
                    504
                    if response.task_status
                    not in {"failed", "cancelled", "timed_out"}
                    else 500
                )
                return JSONResponse(
                    status_code=status_code,
                    content=response.model_dump(mode="json"),
                )
            return response
        orchestrator = _get_orchestrator()
        response = await orchestrator.execute_code(req.code, req.output_formats)
        await claim_request_owner(response.request_id, api_key)
        if not response.success:
            return JSONResponse(
                status_code=500,
                content=response.model_dump(),
            )
        return response
    except StaleBaseRevision as exc:
        return JSONResponse(
            status_code=409,
            content=GenerateResponse(
                request_id=str(uuid.uuid4()),
                success=False,
                error={
                    "type": "RevisionConflict",
                    "message": str(exc),
                },
            ).model_dump(mode="json"),
        )
    except IdempotencyConflict as exc:
        return JSONResponse(
            status_code=409,
            content=GenerateResponse(
                request_id=str(uuid.uuid4()),
                success=False,
                error={
                    "type": "IdempotencyConflict",
                    "message": str(exc),
                },
            ).model_dump(mode="json"),
        )
    except (KeyError, PermissionError) as exc:
        return JSONResponse(
            status_code=403 if isinstance(exc, PermissionError) else 404,
            content=GenerateResponse(
                request_id=str(uuid.uuid4()),
                success=False,
                error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            ).model_dump(mode="json"),
        )
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
