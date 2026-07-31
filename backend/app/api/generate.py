import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.websocket import _get_orchestrator
from app.api.auth import verify_api_key, rate_limiter
from app.api.error_messages import (
    generation_error_http_status,
    public_generation_error,
)
from app.config import settings
from app.models.schemas import GenerateRequest, GenerateResponse, ModifyRequest
from app.principal_context import current_principal
from app.repositories.revisions import StaleBaseRevision
from app.services.run_state import IdempotencyConflict
from app.services.durable_submission import (
    submit_durable_workflow,
    wait_for_compatibility_response,
)
from app.storage.file_ownership import claim_request_owner

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest, request: Request, api_key: str | None = Depends(verify_api_key)):
    """从自然语言描述生成 CAD 模型 (同步, 有总超时上限)"""
    await rate_limiter.check(request, api_key)
    try:
        if settings.durable_api_cutover_enabled:
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
            response = await wait_for_compatibility_response(
                current_principal(),
                submission,
                timeout_seconds=settings.generate_deadline_s,
            )
            if (
                not response.success
                and not response.needs_confirmation
            ):
                status_code = (
                    generation_error_http_status(response.error or {})
                    if response.task_status
                    in {"failed", "cancelled", "timed_out"}
                    else 504
                )
                return JSONResponse(
                    status_code=status_code,
                    content=response.model_dump(mode="json"),
                )
            return response
        orchestrator = _get_orchestrator()
        # Hard overall deadline: planning + N×LLM + N×sandbox can otherwise run for
        # minutes. Fail fast instead of hanging the tester's request.
        generate_kwargs = {}
        if req.manufacturing_profile is not None:
            generate_kwargs["manufacturing_profile"] = req.manufacturing_profile
        response = await asyncio.wait_for(
            orchestrator.generate(req.prompt, req.output_formats, **generate_kwargs),
            timeout=settings.generate_deadline_s,
        )
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
        request_id = str(uuid.uuid4())
        error = public_generation_error(e)
        if error["type"].startswith("Provider"):
            logger.warning("Generate provider failure: %s", error["type"])
        else:
            logger.exception("Generate failed")
        return JSONResponse(
            status_code=generation_error_http_status(error),
            content=GenerateResponse(
                request_id=request_id,
                success=False,
                error=error,
            ).model_dump(),
        )


@router.post("/api/modify", response_model=GenerateResponse)
async def modify(req: ModifyRequest, request: Request, api_key: str | None = Depends(verify_api_key)):
    """基于已有代码修改 CAD 模型"""
    await rate_limiter.check(request, api_key)
    try:
        if settings.durable_api_cutover_enabled:
            submission = await submit_durable_workflow(
                current_principal(),
                project_id=req.project_id,
                branch_id=req.branch_id,
                expected_base_revision_id=req.expected_base_revision_id,
                idempotency_key=req.idempotency_key,
                operation="modify",
                objective=req.prompt,
                output_formats=req.output_formats,
                code=req.code,
            )
            response = await wait_for_compatibility_response(
                current_principal(),
                submission,
                timeout_seconds=settings.generate_deadline_s,
            )
            if (
                not response.success
                and not response.needs_confirmation
            ):
                status_code = (
                    generation_error_http_status(response.error or {})
                    if response.task_status
                    in {"failed", "cancelled", "timed_out"}
                    else 504
                )
                return JSONResponse(
                    status_code=status_code,
                    content=response.model_dump(mode="json"),
                )
            return response
        orchestrator = _get_orchestrator()
        response = await orchestrator.modify(req.code, req.prompt, req.output_formats)
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
        request_id = str(uuid.uuid4())
        error = public_generation_error(e)
        if error["type"].startswith("Provider"):
            logger.warning("Modify provider failure: %s", error["type"])
        else:
            logger.exception("Modify failed")
        return JSONResponse(
            status_code=generation_error_http_status(error),
            content=GenerateResponse(
                request_id=request_id,
                success=False,
                error=error,
            ).model_dump(),
        )
