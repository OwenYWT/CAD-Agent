from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from app.agent.tool_registry import ToolRegistry
from app.agent.tool_types import AgentTool, ToolExecutionContext, ToolExecutionResult
from app.integrations.onshape.service import OnshapeService
from app.models.schemas import OnshapeCreateDocumentRequest, OnshapePublishRequest
from app.storage import history
from app.storage.file_ownership import FileOwnershipError, request_belongs_to


class OnshapeListDocumentsArgs(BaseModel):
    q: str | None = Field(None, max_length=256, description="Optional Onshape document search query.")
    offset: int = Field(0, ge=0, le=1000, description="Pagination offset.")
    limit: int = Field(20, ge=1, le=50, description="Maximum number of documents to return.")


class OnshapeListElementsArgs(BaseModel):
    document_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    workspace_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")


class OnshapeListPartstudioFeaturesArgs(BaseModel):
    document_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    workspace_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    element_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")


class OnshapeCreateDocumentArgs(BaseModel):
    name: str = Field(..., min_length=1, max_length=256, description="Name for the new Onshape document.")
    description: str | None = Field(None, max_length=2000, description="Optional Onshape document description.")
    is_public: bool | None = Field(None, description="Override the deployment default document visibility.")


class OnshapePublishStepArgs(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    document_id: str | None = Field(None, min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    workspace_id: str | None = Field(None, min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    document_name: str | None = Field(None, min_length=1, max_length=256)
    step_filename: str | None = Field(None, min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9._-]+$")
    wait_for_completion: bool = False
    poll_interval_s: float = Field(2.0, ge=0.5, le=10.0)
    timeout_s: float = Field(60.0, ge=1.0, le=300.0)


class OnshapeGetTranslationArgs(BaseModel):
    translation_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")


class OnshapeGetLinksArgs(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")


OnshapeServiceFactory = Callable[[], OnshapeService]


def build_onshape_tools(
    service_factory: OnshapeServiceFactory | None = None,
) -> list[AgentTool]:
    get_service = service_factory or OnshapeService

    async def list_documents(
        args: BaseModel,
        _context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        _require_shared_onshape_access(_context)
        typed = OnshapeListDocumentsArgs.model_validate(args)
        response = await get_service().list_documents(q=typed.q, offset=typed.offset, limit=typed.limit)
        documents = [_summarize_document(item) for item in response.documents if isinstance(item, dict)]
        return ToolExecutionResult(
            tool_name="onshape_list_documents",
            status="success",
            safety_level="read",
            summary={"documents": documents, "count": len(documents), "offset": typed.offset, "limit": typed.limit},
            raw=response.model_dump(),
        )

    async def list_elements(
        args: BaseModel,
        _context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        _require_shared_onshape_access(_context)
        typed = OnshapeListElementsArgs.model_validate(args)
        response = await get_service().list_elements(typed.document_id, typed.workspace_id)
        return ToolExecutionResult(
            tool_name="onshape_list_elements",
            status="success",
            safety_level="read",
            summary={
                "document_id": response["document_id"],
                "workspace_id": response["workspace_id"],
                "elements": response["elements"],
                "count": len(response["elements"]),
            },
            raw=response,
        )

    async def list_partstudio_features(
        args: BaseModel,
        _context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        _require_shared_onshape_access(_context)
        typed = OnshapeListPartstudioFeaturesArgs.model_validate(args)
        response = await get_service().list_partstudio_features(
            typed.document_id,
            typed.workspace_id,
            typed.element_id,
        )
        return ToolExecutionResult(
            tool_name="onshape_list_partstudio_features",
            status="success",
            safety_level="read",
            summary={
                "document_id": response["document_id"],
                "workspace_id": response["workspace_id"],
                "element_id": response["element_id"],
                "features": response["features"],
                "count": len(response["features"]),
            },
            raw=response,
        )

    async def create_document(
        args: BaseModel,
        _context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        _require_shared_onshape_access(_context)
        typed = OnshapeCreateDocumentArgs.model_validate(args)
        response = await get_service().create_document(OnshapeCreateDocumentRequest(**typed.model_dump()))
        payload = response.model_dump()
        return ToolExecutionResult(
            tool_name="onshape_create_document",
            status="success",
            safety_level="write",
            summary={
                "document_id": response.id,
                "name": response.name,
                "default_workspace_id": response.default_workspace_id,
                "web_url": response.web_url,
            },
            raw=payload,
        )

    async def publish_step(
        args: BaseModel,
        _context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        typed = OnshapePublishStepArgs.model_validate(args)
        _require_generated_file_access(typed.request_id, _context)
        if typed.document_id:
            _require_shared_onshape_access(_context)
        response = await get_service().publish_step(
            OnshapePublishRequest(**typed.model_dump()),
            user_id=_context.user_id,
        )
        payload = response.model_dump()
        return ToolExecutionResult(
            tool_name="onshape_publish_step",
            status="success",
            safety_level="write",
            summary={
                "request_id": response.request_id,
                "status": response.status,
                "onshape_url": response.onshape_url,
                "document_id": response.document_id,
                "workspace_id": response.workspace_id,
                "element_id": response.element_id,
                "translation_id": response.translation_id,
            },
            raw=payload,
        )

    async def get_translation(
        args: BaseModel,
        _context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        typed = OnshapeGetTranslationArgs.model_validate(args)
        await _require_translation_access(typed.translation_id, _context)
        response = await get_service().get_translation(typed.translation_id)
        return ToolExecutionResult(
            tool_name="onshape_get_translation",
            status="success",
            safety_level="read",
            summary={
                "translation_id": typed.translation_id,
                "status": str(response.get("requestState") or response.get("state") or response.get("status") or ""),
                "element_ids": response.get("resultElementIds") or response.get("elementIds") or [],
            },
            raw=response,
        )

    async def get_links(
        args: BaseModel,
        _context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        typed = OnshapeGetLinksArgs.model_validate(args)
        response = await get_service().get_links(typed.request_id, user_id=_context.user_id)
        return ToolExecutionResult(
            tool_name="onshape_get_links",
            status="success",
            safety_level="read",
            summary={"request_id": typed.request_id, "links": response, "count": len(response)},
            raw=response,
        )

    async def refresh_latest_link(
        args: BaseModel,
        _context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        typed = OnshapeGetLinksArgs.model_validate(args)
        response = await get_service().refresh_latest_link(typed.request_id, user_id=_context.user_id)
        return ToolExecutionResult(
            tool_name="onshape_refresh_latest_link",
            status="success",
            safety_level="read",
            summary={
                "request_id": response.get("request_id"),
                "status": response.get("status"),
                "onshape_url": response.get("onshape_url"),
                "translation_id": response.get("translation_id"),
                "element_id": response.get("element_id"),
            },
            raw=response,
        )

    return [
        AgentTool(
            name="onshape_list_documents",
            description="Search or list Onshape documents visible to the configured Onshape account.",
            args_model=OnshapeListDocumentsArgs,
            safety_level="read",
            handler=list_documents,
        ),
        AgentTool(
            name="onshape_list_elements",
            description="List elements in an Onshape document workspace, including Part Studios and Assemblies.",
            args_model=OnshapeListElementsArgs,
            safety_level="read",
            handler=list_elements,
        ),
        AgentTool(
            name="onshape_list_partstudio_features",
            description="List FeatureScript features in an Onshape Part Studio.",
            args_model=OnshapeListPartstudioFeaturesArgs,
            safety_level="read",
            handler=list_partstudio_features,
        ),
        AgentTool(
            name="onshape_create_document",
            description="Create a new Onshape document using the configured Onshape account.",
            args_model=OnshapeCreateDocumentArgs,
            safety_level="write",
            handler=create_document,
            requires_confirmation=True,
        ),
        AgentTool(
            name="onshape_publish_step",
            description="Upload a generated STEP/STP artifact into a new or existing Onshape document.",
            args_model=OnshapePublishStepArgs,
            safety_level="write",
            handler=publish_step,
            requires_confirmation=True,
            timeout_s=120.0,
        ),
        AgentTool(
            name="onshape_get_translation",
            description="Read the status of a previously recorded Onshape translation job.",
            args_model=OnshapeGetTranslationArgs,
            safety_level="read",
            handler=get_translation,
        ),
        AgentTool(
            name="onshape_get_links",
            description="List Onshape publish links recorded for a CAD-Agent request.",
            args_model=OnshapeGetLinksArgs,
            safety_level="read",
            handler=get_links,
        ),
        AgentTool(
            name="onshape_refresh_latest_link",
            description="Refresh the latest recorded Onshape translation status for a CAD-Agent request.",
            args_model=OnshapeGetLinksArgs,
            safety_level="read",
            handler=refresh_latest_link,
        ),
    ]


def build_onshape_tool_registry(
    service_factory: OnshapeServiceFactory | None = None,
) -> ToolRegistry:
    return ToolRegistry(build_onshape_tools(service_factory))


def _summarize_document(raw: dict[str, Any]) -> dict[str, Any]:
    default_workspace = raw.get("defaultWorkspace") if isinstance(raw.get("defaultWorkspace"), dict) else {}
    workspace_id = (
        raw.get("defaultWorkspaceId")
        or raw.get("workspaceId")
        or raw.get("wid")
        or default_workspace.get("id")
        or default_workspace.get("workspaceId")
    )
    document_id = raw.get("id") or raw.get("documentId") or raw.get("did")
    return {
        "document_id": str(document_id or ""),
        "name": str(raw.get("name") or ""),
        "default_workspace_id": str(workspace_id or ""),
    }


def _require_shared_onshape_access(context: ToolExecutionContext) -> None:
    if context.allow_shared_onshape:
        return
    raise PermissionError("Only administrators can browse or write shared Onshape documents")


def _require_generated_file_access(request_id: str, context: ToolExecutionContext) -> None:
    try:
        allowed = request_belongs_to(request_id, context.auth_principal)
    except FileOwnershipError as exc:
        raise PermissionError(str(exc)) from exc
    if not allowed:
        raise PermissionError("Generated STEP file is not available to this principal")


async def _require_translation_access(translation_id: str, context: ToolExecutionContext) -> None:
    link = await history.get_onshape_link_by_translation(translation_id, user_id=context.user_id)
    if not link:
        raise PermissionError("Onshape translation is not available to this principal")
