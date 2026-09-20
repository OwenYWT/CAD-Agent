"""Authenticated document operations, checkpoints, deltas and collaboration."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

from app.api.auth import get_current_user, get_durable_principal, resolve_ws_principal, verify_ws_token, websocket_auth_token, rate_limiter
from app.services.document_sharing import create_review_invite, accept_review_invite, workspace_principal, project_members, change_project_member
from app.services.feature_annotations import save_annotation
from app.services.feature_leases import FeatureLeaseConflict, acquire_feature_lease, release_feature_lease, assert_operation_lease_access
from app.services.document_rebase import resolve_parameter_rebase
from app.services.document_geometry import scene_mesh
from app.services.scene_jobs import request_scene
from app.services.document_branches import list_document_branches, fork_document, compare_branches, merge_document
from app.services.document_engineering import submit_engineering, engineering_tasks, engineering_result
from app.freecad.engineering_contracts import EngineeringSubmission
from app.freecad.release_contracts import ReleaseSubmission
from app.services.document_releases import submit_release, document_releases, release_result
from app.execution.canonical import canonical_sha256
from app.domain.identity import user_principal
from app.db import tenant_transaction
from app.domain.identity import PrincipalContext
from app.domain.projects import Permission
from app.freecad.state_contract import ParameterStateError, compile_parameter_operation_plan, read_verified_state_artifact
from app.freecad.inspection import InspectionRequest, inspect_state
from app.freecad.selection import SelectionContextV1
from app.object_store import get_object
from app.repositories.revisions import StaleBaseRevision
from app.services.cloud_documents import (DocumentConflict, authorized_document, checkpoint, document_snapshot, document_revision_view,
    document_events, collaboration_snapshot, touch_presence, add_comment)
from app.services.durable_submission import submit_durable_workflow
from app.services.operation_resolution import (OperationResolutionError, load_revision_source_inventory,
    resolve_rest_generate_submission, resolve_rest_modify_submission)
from app.workflows.temporal import FreeCADStructuredModificationV1
from app.services.run_state import IdempotencyConflict
from app.temporal_client import TemporalWorkerUnavailable

from app.models.document_requests import OperationRequest
from app.services.document_operations import submit_document_operation

router = APIRouter(prefix="/api/documents", tags=["documents"])


class FeatureAnnotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    annotation_id: UUID
    revision_id: UUID
    expected_version: int = Field(ge=0, strict=True)
    role: str | None = Field(default=None, max_length=120)
    intent: str | None = Field(default=None, max_length=2000)

    @field_validator("role", "intent")
    @classmethod
    def trim_text(cls, value):
        return (value.strip() or None) if value is not None else None


class DocumentInspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: UUID
    query: InspectionRequest


class BranchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    expected_revision_id: UUID
    expected_state_version: int = Field(ge=0, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=120)


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_document_id: UUID
    source_revision_id: UUID
    source_state_version: int = Field(ge=0, strict=True)
    target_revision_id: UUID
    target_state_version: int = Field(ge=0, strict=True)
    mode: Literal['parameters','source_geometry']
    comparison_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    idempotency_key: str = Field(min_length=1, max_length=120)


class FeatureLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feature_id: UUID
    client_id: UUID
    revision_id: UUID
    lease_token: UUID | None = None


class MemberRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_role: Literal['viewer','editor']
    role: Literal['viewer','editor']
logger = logging.getLogger(__name__)




class PresenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: UUID
    selected_feature_id: UUID | None = None


class CommentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment_id: UUID
    revision_id: UUID
    feature_id: UUID | None = None
    body: str = Field(min_length=1, max_length=4000)

    @field_validator("body")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("评论不能为空")
        return value.strip()


class AcceptInviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: UUID
    token: str = Field(min_length=32, max_length=128)


@router.post("/{document_id}/invitations", status_code=201)
async def invite(document_id: UUID, role: Literal['viewer','editor'] = Query(default='viewer'), principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return await create_review_invite(principal, document_id, role=role)
    except Exception as exc:
        raise public_error(exc) from exc


@router.post("/{document_id}/invitations/accept")
async def accept_invite(document_id: UUID, body: AcceptInviteRequest, user: dict = Depends(get_current_user)):
    try:
        return await accept_review_invite(user_principal(str(user["id"])), document_id, body.tenant_id, body.token)
    except Exception as exc:
        raise public_error(exc) from exc


@router.get("/{document_id}/members")
async def list_members(document_id: UUID, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return {"members":await project_members(principal,document_id)}
    except Exception as exc:
        raise public_error(exc) from exc


@router.patch("/{document_id}/members/{member_id}")
async def update_member(document_id: UUID, member_id: UUID, body: MemberRoleRequest, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return await change_project_member(principal,document_id,member_id,**body.model_dump())
    except Exception as exc:
        raise public_error(exc) from exc


@router.delete("/{document_id}/members/{member_id}",status_code=204)
async def remove_member(document_id: UUID, member_id: UUID, expected_role: Literal['viewer','editor'] = Query(), principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        await change_project_member(principal,document_id,member_id,expected_role=expected_role)
    except Exception as exc:
        raise public_error(exc) from exc
    return Response(status_code=204)


def public_error(exc: Exception) -> HTTPException:
    if isinstance(exc, TemporalWorkerUnavailable):
        return HTTPException(503, detail={"code": "workflow_worker_unavailable",
            "message": "建模服务暂不可用，请稍后重试；本次尚未创建任务。", "retryable": True})
    if isinstance(exc, (DocumentConflict, StaleBaseRevision, IdempotencyConflict, FeatureLeaseConflict)):
        return HTTPException(409, detail={"code": getattr(exc, "code", "document_conflict"), "message": str(exc), "retryable": False})
    if isinstance(exc, KeyError):
        return HTTPException(404, detail="未找到文档或版本")
    if isinstance(exc, PermissionError):
        return HTTPException(403, detail="无权执行此文档操作")
    if isinstance(exc, (ParameterStateError, OperationResolutionError, ValueError)):
        return HTTPException(422, detail={"code": getattr(exc, "code", "document_input_invalid"), "message": str(exc), "retryable": False})
    raise exc


@router.get("/{document_id}")
async def get_document(document_id: UUID, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return await document_snapshot(principal, document_id)
    except Exception as exc:
        raise public_error(exc) from exc


@router.get("/{document_id}/events")
async def get_events(document_id: UUID, after: int = Query(default=0, ge=0), principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return {"events": await document_events(principal, document_id, after)}
    except Exception as exc:
        raise public_error(exc) from exc


@router.get("/{document_id}/revisions/{revision_id}")
async def view_revision(document_id: UUID, revision_id: UUID, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return await document_revision_view(principal, document_id, revision_id)
    except Exception as exc:
        raise public_error(exc) from exc


@router.get("/{document_id}/branches")
async def branches(document_id: UUID, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return await list_document_branches(principal, document_id)
    except Exception as exc:
        raise public_error(exc) from exc


@router.post("/{document_id}/branches", status_code=202)
async def create_branch(document_id: UUID, body: BranchRequest, request: Request,
                        principal: PrincipalContext = Depends(get_durable_principal)):
    await rate_limiter.check(request)
    try:
        return await fork_document(principal, document_id, **body.model_dump())
    except Exception as exc:
        raise public_error(exc) from exc


@router.get("/{document_id}/compare/{source_document_id}")
async def compare(document_id: UUID, source_document_id: UUID, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return await compare_branches(principal,document_id,source_document_id)
    except Exception as exc:
        raise public_error(exc) from exc


@router.post("/{document_id}/merges", status_code=202)
async def merge(document_id: UUID, body: MergeRequest, request: Request,
                principal: PrincipalContext = Depends(get_durable_principal)):
    await rate_limiter.check(request)
    try:
        return await merge_document(principal,document_id,**body.model_dump())
    except Exception as exc:
        raise public_error(exc) from exc


@router.put("/{document_id}/features/{feature_id}/annotation")
async def feature_annotation(document_id: UUID, feature_id: UUID, body: FeatureAnnotationRequest,
                             principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return await save_annotation(principal, document_id, feature_id, **body.model_dump())
    except Exception as exc:
        raise public_error(exc) from exc


@router.post("/{document_id}/leases")
async def feature_lease(document_id: UUID, body: FeatureLeaseRequest,
                        principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return await acquire_feature_lease(principal, document_id, **body.model_dump())
    except Exception as exc:
        raise public_error(exc) from exc


@router.delete("/{document_id}/leases/{token}", status_code=204)
async def release_lease(document_id: UUID, token: UUID, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        await release_feature_lease(principal, document_id, token)
    except Exception as exc:
        raise public_error(exc) from exc
    return Response(status_code=204)


@router.get("/{document_id}/collaboration")
async def get_collaboration(document_id: UUID, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return await collaboration_snapshot(principal, document_id)
    except Exception as exc:
        raise public_error(exc) from exc


@router.post("/{document_id}/inspect")
async def inspect_document(document_id: UUID, body: DocumentInspectionRequest,
                           principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        projection = await checkpoint(principal, document_id, body.revision_id)
        if not projection["state"]:
            raise ValueError("此版本没有可验证的内核检查点")
        async with tenant_transaction(principal.tenant_id, principal.principal_id) as conn:
            await authorized_document(conn, principal, document_id)
            rows = [dict(a) for a in (await conn.execute(text("SELECT * FROM artifacts WHERE id=:id"),
                {"id": UUID(projection["state"]["artifact_id"])})).mappings()]
        state, digest = await read_verified_state_artifact(rows)
        return {**inspect_state(state, body.query), "revision_id": str(body.revision_id), "artifact_sha256": digest}
    except Exception as exc:
        raise public_error(exc) from exc


@router.post("/{document_id}/presence", status_code=204)
async def presence(document_id: UUID, body: PresenceRequest, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        await touch_presence(principal, document_id, body.client_id, body.selected_feature_id)
    except Exception as exc:
        raise public_error(exc) from exc
    return Response(status_code=204)


@router.post("/{document_id}/comments", status_code=201)
async def comment(document_id: UUID, body: CommentRequest, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return await add_comment(principal, document_id, **body.model_dump())
    except Exception as exc:
        raise public_error(exc) from exc


@router.post("/{document_id}/operations", status_code=202)
async def operation(document_id: UUID, body: OperationRequest, request: Request, principal: PrincipalContext = Depends(get_durable_principal)):
    await rate_limiter.check(request)
    try:
        return await submit_document_operation(document_id, body, principal)
    except Exception as exc:
        raise public_error(exc) from exc


@router.post('/{document_id}/releases', status_code=202)
async def publish_release(document_id: UUID, body: ReleaseSubmission, request: Request,
                          principal: PrincipalContext = Depends(get_durable_principal)):
    await rate_limiter.check(request)
    try:
        return await submit_release(principal, document_id, body)
    except Exception as exc:
        raise public_error(exc) from exc


@router.get('/{document_id}/releases')
async def releases(document_id: UUID, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return await document_releases(principal, document_id)
    except Exception as exc:
        raise public_error(exc) from exc


@router.get('/{document_id}/releases/{release_id}')
async def released_evidence(document_id: UUID, release_id: UUID, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return await release_result(principal, document_id, release_id)
    except Exception as exc:
        raise public_error(exc) from exc


@router.post('/{document_id}/engineering', status_code=202)
async def compute(document_id: UUID, body: EngineeringSubmission, request: Request,
                  principal: PrincipalContext = Depends(get_durable_principal)):
    await rate_limiter.check(request)
    try:
        return await submit_engineering(principal, document_id, body)
    except Exception as exc:
        raise public_error(exc) from exc


@router.get('/{document_id}/engineering')
async def computations(document_id: UUID, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return await engineering_tasks(principal, document_id)
    except Exception as exc:
        raise public_error(exc) from exc


@router.get('/{document_id}/engineering/{workflow_id}')
async def computation_result(document_id: UUID, workflow_id: UUID, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        return await engineering_result(principal, document_id, workflow_id)
    except Exception as exc:
        raise public_error(exc) from exc


@router.get("/{document_id}/artifacts/{artifact_id}")
async def artifact(document_id: UUID, artifact_id: UUID, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        async with tenant_transaction(principal.tenant_id, principal.principal_id) as conn:
            await authorized_document(conn, principal, document_id)
            row = (await conn.execute(text("""SELECT a.* FROM artifacts a JOIN project_revisions r ON r.id=a.revision_id
                WHERE r.branch_id=:doc AND a.id=:id"""), {"doc": document_id, "id": artifact_id})).mappings().one_or_none()
            if row is None:
                raise KeyError(artifact_id)
            if row["artifact_kind"] not in {"state", "stl", "engineering_report", "engineering_field", "release_bom_json", "release_manifest"}:
                await authorized_document(conn, principal, document_id, Permission.EXPORT_ARTIFACT)
        payload = await get_object(row["object_key"])
        if len(payload) != row["size_bytes"] or hashlib.sha256(payload).hexdigest() != row["sha256"]:
            raise HTTPException(503, detail="文档文件校验失败")
        return Response(payload, media_type=row["content_type"], headers={"ETag": f'"{row["sha256"]}"', "Cache-Control": "private, max-age=3600"})
    except Exception as exc:
        raise public_error(exc) from exc


@router.get('/{document_id}/scenes/{revision_id}')
async def get_scene(document_id: UUID, revision_id: UUID, response: Response, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        scene = await request_scene(principal, document_id, revision_id)
        response.status_code = 200 if scene.get('schema_version') == 'cad-scene.v1' else 202
        return scene
    except Exception as exc:
        raise public_error(exc) from exc


class SceneRetryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workflow_run_id: UUID


@router.post('/{document_id}/scenes/{revision_id}/retry')
async def retry_scene(document_id: UUID, revision_id: UUID, body: SceneRetryRequest, response: Response,
                      request: Request, principal: PrincipalContext = Depends(get_durable_principal)):
    await rate_limiter.check(request)
    try:
        scene = await request_scene(principal, document_id, revision_id, retry_workflow_id=body.workflow_run_id)
        response.status_code = 200 if scene.get('schema_version') == 'cad-scene.v1' else 202
        return scene
    except Exception as exc:
        raise public_error(exc) from exc


@router.get('/{document_id}/scenes/{revision_id}/meshes/{sha256}')
async def get_scene_mesh(document_id: UUID, revision_id: UUID, sha256: str, principal: PrincipalContext = Depends(get_durable_principal)):
    try:
        payload = await scene_mesh(principal, document_id, revision_id, sha256)
        return Response(payload, media_type='model/stl', headers={'ETag':f'"{sha256}"', 'Cache-Control':'private, max-age=3600'})
    except Exception as exc:
        raise public_error(exc) from exc


@router.websocket("/{document_id}/stream")
async def stream(websocket: WebSocket, document_id: UUID):
    token, protocol = websocket_auth_token(websocket)
    if not await verify_ws_token(token):
        await websocket.close(code=4003)
        return
    principal = await resolve_ws_principal(token)
    if principal is None:
        await websocket.close(code=4003)
        return
    try:
        workspace = websocket.query_params.get("workspace")
        if workspace:
            principal = await workspace_principal(principal, UUID(workspace))
        snapshot = await document_snapshot(principal, document_id)
    except (KeyError, PermissionError, ValueError):
        await websocket.close(code=4003)
        return
    await websocket.accept(subprotocol=protocol)
    try:
        # A reconnect gets one full JSON snapshot, then immutable ordered deltas.
        # No binary checkpoint is sent and a missed cursor never silently skips state.
        await websocket.send_json({"type": "document_snapshot", "data": snapshot})
        cursor = snapshot["event_sequence"]
        previous = None
        while True:
            if not await verify_ws_token(token):
                await websocket.close(code=4003)
                return
            for event in await document_events(principal, document_id, cursor):
                await websocket.send_json({"type": "document_event", "data": event})
                cursor = event["sequence"]
            current = jsonable_encoder(await collaboration_snapshot(principal, document_id))
            marker = json.dumps(current, sort_keys=True)
            if marker != previous:
                await websocket.send_json({"type": "document_collaboration", "data": current})
                previous = marker
            # Reading allows prompt cleanup on disconnect, also without an edit.
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=1)
                if message["type"] == "websocket.disconnect":
                    return
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        return
    except (KeyError, PermissionError):
        await websocket.close(code=4003)
    except Exception:
        logger.exception("Document stream failed")
        await websocket.close(code=1011, reason="Document stream failed")
