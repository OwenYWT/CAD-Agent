import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

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
from app.services.operation_resolution import (
    OperationResolutionError,
    load_revision_source_inventory,
    resolve_rest_generate_submission,
    resolve_rest_modify_submission,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest, request: Request, api_key: str | None = Depends(verify_api_key)):
    """从自然语言描述生成 CAD 模型 (同步, 有总超时上限)"""
    await rate_limiter.check(request, api_key)
    try:
        resolution = resolve_rest_generate_submission(
            base_revision_id=req.expected_base_revision_id,
            output_formats=req.output_formats,
        )
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
            modeling_backend=resolution.modeling_backend,
            operation_context=resolution.operation_context,
        )
        response = await wait_for_compatibility_response(
            current_principal(),
            submission,
            timeout_seconds=settings.generate_deadline_s,
        )
        if not response.success and not response.needs_confirmation:
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
        principal = current_principal()
        inventory = await load_revision_source_inventory(
            principal,
            project_id=req.project_id,
            revision_id=req.expected_base_revision_id,
        )
        resolution = resolve_rest_modify_submission(
            requested_backend=req.modeling_backend,
            base_revision_id=req.expected_base_revision_id,
            inventory=inventory,
            request_code=req.code,
        )
        submission = await submit_durable_workflow(
            principal,
            project_id=req.project_id,
            branch_id=req.branch_id,
            expected_base_revision_id=req.expected_base_revision_id,
            idempotency_key=req.idempotency_key,
            operation="modify",
            objective=req.prompt,
            output_formats=req.output_formats,
            code=resolution.existing_code,
            modeling_backend=resolution.modeling_backend,
            operation_context=resolution.operation_context,
            **({"expected_state_version": req.expected_state_version} if req.expected_state_version is not None else {}),
            **({"selection_context": req.selection_context} if req.selection_context is not None else {}),
        )
        response = await wait_for_compatibility_response(
            principal,
            submission,
            timeout_seconds=settings.generate_deadline_s,
        )
        if not response.success and not response.needs_confirmation:
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
    except OperationResolutionError as exc:
        return JSONResponse(
            status_code=422,
            content=GenerateResponse(
                request_id=str(uuid.uuid4()),
                success=False,
                error=exc.public_error(),
            ).model_dump(mode="json"),
        )
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
