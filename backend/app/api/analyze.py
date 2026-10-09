"""Engineering-check API backed by the durable MCAD control plane."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.auth import get_durable_principal, verify_api_key
from app.config import settings
from app.db import tenant_transaction
from app.execution.canonical import canonical_sha256
from app.models.schemas import AnalyzeRequest, DesignAnalysisResponse
from app.principal_context import current_principal
from app.services.design_analysis import build_design_analysis_response
from app.storage.file_ownership import request_belongs_to
from app.workflows.temporal import start_mcad_check_workflow

logger = logging.getLogger(__name__)
router = APIRouter()

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


async def _durable_source(
    request_id: str,
) -> tuple[UUID, UUID, UUID]:
    try:
        source_workflow_id = UUID(request_id)
    except ValueError as exc:
        raise HTTPException(404, "Request ID not found") from exc
    principal = current_principal()
    async with tenant_transaction(
        principal.tenant_id,
        principal.principal_id,
    ) as connection:
        row = (
            await connection.execute(
                text(
                    """
                    SELECT a.project_id, a.revision_id
                    FROM artifacts a
                    JOIN project_revisions revision
                      ON revision.tenant_id=a.tenant_id
                     AND revision.project_id=a.project_id
                     AND revision.id=a.revision_id
                     AND revision.source_workflow_run_id=a.workflow_run_id
                    WHERE a.tenant_id=:tenant_id
                      AND a.workflow_run_id=:workflow_run_id
                      AND a.artifact_kind='stl'
                    ORDER BY a.created_at DESC, a.id DESC
                    LIMIT 1
                    """
                ),
                {
                    "tenant_id": principal.tenant_id,
                    "workflow_run_id": source_workflow_id,
                },
            )
        ).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "No STL file found for this request")
    return source_workflow_id, row["project_id"], row["revision_id"]


async def _run_durable_check(
    request_id: str,
    body: AnalyzeRequest,
    response: Response,
) -> DesignAnalysisResponse:
    source_workflow_id, project_id, source_revision_id = (
        await _durable_source(request_id)
    )
    principal = current_principal()
    from app.services.engineering_checks import authorize_check_source
    await authorize_check_source(principal, source_workflow_id)
    if body.source_revision_id and body.source_revision_id != str(source_revision_id):
        raise HTTPException(409, "当前检查模型的版本已变化")
    async with tenant_transaction(principal.tenant_id, principal.principal_id) as connection:
        source_payload = await connection.scalar(text("""
            SELECT request_payload FROM workflow_runs
            WHERE tenant_id=:tenant AND id=:workflow
        """), {"tenant": principal.tenant_id, "workflow": source_workflow_id})
    profile = (source_payload or {}).get("manufacturing_profile") or {}
    process = body.process or profile.get("process")
    material = body.material or profile.get("material")
    if not process:
        raise HTTPException(422, "此版本未记录制造工艺，请明确选择检查工艺")
    body_payload = body.model_dump(mode="json", exclude={"idempotency_key", "asynchronous"})
    body_payload.update(process=process, material=material)
    workflow_run_id, handle = await start_mcad_check_workflow(
        tenant_id=principal.tenant_id,
        project_id=project_id,
        principal_id=principal.principal_id,
        source_workflow_run_id=source_workflow_id,
        source_revision_id=source_revision_id,
        idempotency_key=(
            f"check-v2:{principal.principal_id}:{source_workflow_id}:"
            f"{body.idempotency_key or canonical_sha256(body_payload)}"
        ),
        configuration_scoped_idempotency=body.idempotency_key is None,
        code=body.code,
        description=body.description,
        process=process,
        material=material,
        timeout_seconds=min(
            3600,
            max(1, int(settings.sandbox_timeout_s * 2)),
        ),
    )
    response.headers["X-Workflow-Run-ID"] = str(workflow_run_id)
    if handle is None or body.asynchronous:
        return JSONResponse(status_code=202,
            headers={"X-Workflow-Run-ID": str(workflow_run_id)},
            content={"workflow_run_id": str(workflow_run_id), "task_status": "pending",
                     "message": "检查请求已持久保存，等待工作流派发。"})
    try:
        result = await asyncio.wait_for(
            handle.result(),
            timeout=settings.generate_deadline_s,
        )
    except TimeoutError:
        return JSONResponse(status_code=202,
            headers={"X-Workflow-Run-ID": str(workflow_run_id)},
            content={"workflow_run_id": str(workflow_run_id), "task_status": "pending",
                     "message": "工程检查仍在后台运行。"})
    except Exception as exc:
        logger.error(
            "Durable analysis failed for %s (workflow %s): %s",
            request_id,
            workflow_run_id,
            type(exc).__name__,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="工程检查失败，请通过任务状态查看错误并重试。",
        ) from exc
    try:
        analysis = DesignAnalysisResponse.model_validate(result["analysis"])
        analysis.source_revision_id = str(source_revision_id)
        analysis.process = process
        analysis.material = material
        return analysis
    except (KeyError, TypeError, ValueError) as exc:
        logger.error(
            "Durable analysis returned an invalid projection for workflow %s",
            workflow_run_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="工程检查结果格式无效。",
        ) from exc


async def _run_legacy_check(
    request_id: str,
    body: AnalyzeRequest,
) -> DesignAnalysisResponse:
    storage = Path(settings.file_storage_dir) / request_id
    if not storage.exists():
        raise HTTPException(404, "Request ID not found")
    stl_path = next(storage.glob("*.stl"), None)
    step_path = next(storage.glob("*.step"), None)
    if stl_path is None:
        raise HTTPException(404, "No STL file found for this request")

    from app.validation.dfm_analyzer import DFMAnalyzer

    try:
        result = await DFMAnalyzer().analyze(
            stl_path=stl_path,
            code=body.code,
            description=body.description,
            process=body.process,
            step_path=step_path,
            material=body.material,
        )
        return build_design_analysis_response(result)
    except Exception as exc:
        logger.error(
            "Legacy analysis failed for %s: %s",
            request_id,
            type(exc).__name__,
            exc_info=True,
        )
        raise HTTPException(500, "Analysis failed") from exc
    finally:
        render_dir = stl_path.parent / "dfm_renders"
        if render_dir.exists():
            shutil.rmtree(render_dir, ignore_errors=True)


@router.post("/analyze/{request_id}", response_model=DesignAnalysisResponse)
async def analyze_design(
    request_id: str,
    body: AnalyzeRequest,
    response: Response,
    credential=Depends(verify_api_key),
):
    if not _SAFE_ID_RE.fullmatch(request_id):
        raise HTTPException(400, "Invalid request_id")
    if not await request_belongs_to(request_id, credential):
        raise HTTPException(404, "Request ID not found")
    if settings.durable_control_plane_enabled:
        from app.services.run_state import IdempotencyConflict
        try:
            return await _run_durable_check(request_id, body, response)
        except IdempotencyConflict as exc:
            raise HTTPException(409, "检查请求标识已用于不同的输入") from exc
    return await _run_legacy_check(request_id, body)


@router.get("/analyze/tasks/{workflow_run_id}")
async def engineering_check_result(workflow_run_id: UUID, principal=Depends(get_durable_principal)):
    from app.services.engineering_checks import read_engineering_check
    try:
        return await read_engineering_check(principal, workflow_run_id)
    except KeyError as exc:
        raise HTTPException(404, "未找到检查任务") from exc
    except PermissionError as exc:
        raise HTTPException(403, "无权读取该检查任务") from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
