"""LLM-backed, fail-closed planner for one typed Fusion CAD action."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from pydantic import Field, ValidationError, model_validator

from app.config import Settings, make_llm_client, settings

from .agent_contract import AgentPlanResponse, AgentTurnRequest
from .contract import (
    CAD_ACTION_ADAPTER,
    ComponentTarget,
    CreateChamferAction,
    CreateExtrudeAction,
    CreateFilletAction,
    CreateHoleAction,
    CreateSketchAction,
    EntityPlane,
    EntityTarget,
    ExportAction,
    JsonValue,
    SaveAsAction,
    StrictModel,
    UpdateEntityPropertiesAction,
    UpdateFeatureParameterAction,
    UpdateParameterAction,
)
from .policy import classify_risk


class AgentPlanningError(Exception):
    """A stable planner failure safe to map at the HTTP boundary."""

    def __init__(self, message: str, *, code: str = "AGENT_PLAN_INVALID", http_status: int = 422):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


class _ModelPlan(StrictModel):
    status: str
    action: dict[str, JsonValue] | None = None
    question: str | None = Field(default=None, min_length=1, max_length=1_000)
    reason: str | None = Field(default=None, min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def valid_shape(self) -> "_ModelPlan":
        if self.status not in {"proposed", "needs_clarification", "no_action"}:
            raise ValueError("status must be proposed, needs_clarification, or no_action")
        if self.status == "proposed" and self.action is None:
            raise ValueError("proposed requires one action")
        if self.status != "proposed" and self.action is not None:
            raise ValueError("only proposed may include an action")
        if self.status == "needs_clarification" and not self.question:
            raise ValueError("needs_clarification requires a question")
        if self.status == "no_action" and not self.reason:
            raise ValueError("no_action requires a reason")
        return self


_FORBIDDEN_MODEL_KEYS = {
    "source",
    "source_code",
    "code",
    "script",
    "module",
    "callable",
    "command",
    "shell",
    "function",
    "method",
    "attribute",
    "attribute_path",
    "python",
}


def _reject_executable_shape(value: Any) -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            if key in _FORBIDDEN_MODEL_KEYS:
                raise AgentPlanningError("model output contains an arbitrary-code or dynamic-dispatch field")
            _reject_executable_shape(child)
    elif isinstance(value, list):
        for child in value:
            _reject_executable_shape(child)


@dataclass(frozen=True)
class _SemanticRef:
    kind: str
    component_id: str | None


class _ContextIndex:
    def __init__(self, turn: AgentTurnRequest):
        context = turn.context
        self.document_id = context.document.document_id if context.document else None
        self.root_component_id = context.design.root_component_id if context.design else None
        self.components = {item.id: item for item in context.components}
        if self.root_component_id and self.root_component_id not in self.components:
            self.components[self.root_component_id] = None
        self.parameters = {item.id: item for item in context.parameters}
        self.features = {item.id: item for item in context.features}
        self.sketches = {item.id: item for item in context.sketches}
        self.bodies = {item.id: item for item in context.bodies}
        self.occurrences = {item.id: item for item in context.occurrences}
        self.materials = {(item.library_id, item.id): item for item in context.materials}
        self.semantic: dict[str, list[_SemanticRef]] = {}

        for collection, kind in (
            (context.components, "component"),
            (context.occurrences, "occurrence"),
            (context.sketches, "sketch"),
            (context.features, "feature"),
            (context.bodies, "body"),
            (context.assembly, "assembly"),
        ):
            for item in collection:
                component_id = item.id if kind == "component" else item.component_id
                self._add(item.id, kind, component_id)
                self._walk_vendor(item.vendor_extensions, component_id=component_id, hint=None)
        for item in context.selection:
            semantic_kind = item.kind.strip().lower().replace("-", "_").replace(" ", "_")
            self._add(item.id, semantic_kind, item.component_id)
            self._walk_vendor(item.vendor_extensions, component_id=item.component_id, hint=None)
        self._walk_vendor(context.vendor_extensions, component_id=None, hint=None)

        self.folder_ids: set[str] = set()
        if context.cloud and context.cloud.folder_id:
            self.folder_ids.add(context.cloud.folder_id)
        self.folder_ids.update(self._ids("folder"))

    def _add(self, entity_id: str, kind: str, component_id: str | None) -> None:
        refs = self.semantic.setdefault(entity_id, [])
        ref = _SemanticRef(kind=kind, component_id=component_id)
        if ref not in refs:
            refs.append(ref)

    @staticmethod
    def _hint(key: str, inherited: str | None) -> str | None:
        lowered = key.lower()
        for marker, kind in (
            ("profile", "profile"),
            ("edge", "edge"),
            ("sketch_point", "sketch_point"),
            ("point", "sketch_point"),
            ("plane", "plane"),
            ("folder", "folder"),
            ("face", "face"),
        ):
            if marker in lowered:
                return kind
        return inherited

    def _walk_vendor(self, value: JsonValue, *, component_id: str | None, hint: str | None) -> None:
        if isinstance(value, dict):
            local_component = value.get("component_id")
            if isinstance(local_component, str):
                component_id = local_component
            explicit_kind = value.get("kind")
            if isinstance(explicit_kind, str):
                hint = explicit_kind.lower()
            explicit_id = value.get("id")
            if hint and isinstance(explicit_id, str):
                self._add(explicit_id, hint, component_id)
            for key, child in value.items():
                child_hint = self._hint(str(key), hint)
                if child_hint and isinstance(child, str) and str(key).lower().endswith(("id", "_id")):
                    self._add(child, child_hint, component_id)
                else:
                    self._walk_vendor(child, component_id=component_id, hint=child_hint)
        elif isinstance(value, list):
            for child in value:
                if hint and isinstance(child, str):
                    self._add(child, hint, component_id)
                else:
                    self._walk_vendor(child, component_id=component_id, hint=hint)

    def _ids(self, kind: str) -> set[str]:
        return {entity_id for entity_id, refs in self.semantic.items() if any(ref.kind == kind for ref in refs)}

    def require_document(self, document_id: str) -> None:
        if not self.document_id or document_id != self.document_id:
            raise AgentPlanningError("action document_id is not owned by the reported context")

    def require_component(self, component_id: str) -> None:
        if component_id not in self.components:
            raise AgentPlanningError("action component_id is not owned by the reported context")

    def require_semantic(self, entity_id: str, kinds: Iterable[str], component_id: str | None) -> None:
        allowed = set(kinds)
        matches = [ref for ref in self.semantic.get(entity_id, []) if ref.kind in allowed]
        if not matches:
            raise AgentPlanningError(f"action ID {entity_id!r} is not present with the required semantic kind in context")
        if component_id is not None and not any(
            ref.component_id == component_id for ref in matches
        ):
            raise AgentPlanningError(f"action ID {entity_id!r} does not belong to the target component")


def validate_action_against_context(action, turn: AgentTurnRequest) -> None:
    """Validate every ID-bearing v1 field against the supplied context summary."""

    if turn.context.truncated:
        raise AgentPlanningError("reported context is truncated; semantic ID ownership cannot be proven")
    index = _ContextIndex(turn)
    target = action.target
    index.require_document(target.document_id)

    component_id = target.component_id if isinstance(target, (ComponentTarget, EntityTarget)) else None
    if component_id is not None:
        index.require_component(component_id)

    if isinstance(action, (UpdateParameterAction, UpdateFeatureParameterAction)):
        parameter = index.parameters.get(target.parameter_id)
        if parameter is None:
            raise AgentPlanningError("parameter_id is not owned by the reported context")
        if parameter.component_id != target.component_id:
            raise AgentPlanningError("parameter does not belong to the target component")
        if isinstance(action, UpdateFeatureParameterAction):
            feature = index.features.get(target.feature_id)
            if feature is None or feature.component_id != target.component_id:
                raise AgentPlanningError("feature does not belong to the target component")
            if parameter.created_by_id != target.feature_id:
                raise AgentPlanningError("feature parameter ownership is not proven by the reported context")

    if isinstance(action, CreateSketchAction) and isinstance(action.plane, EntityPlane):
        index.require_semantic(
            action.plane.entity_id,
            {"plane", "face", "brepface", "construction_plane", "constructionplane"},
            target.component_id,
        )

    if isinstance(action, CreateExtrudeAction):
        index.require_semantic(action.profile_id, {"profile"}, target.component_id)
        for body_id in action.participant_body_ids:
            index.require_semantic(body_id, {"body"}, target.component_id)

    if isinstance(action, CreateHoleAction):
        for point_id in action.sketch_point_ids:
            index.require_semantic(point_id, {"sketch_point"}, target.component_id)

    if isinstance(action, (CreateFilletAction, CreateChamferAction)):
        for edge_id in action.edge_ids:
            index.require_semantic(edge_id, {"edge"}, target.component_id)

    if isinstance(action, UpdateEntityPropertiesAction):
        index.require_semantic(target.entity_id, {target.entity_kind}, target.component_id)
        if action.properties.material is not None:
            material = action.properties.material
            if (material.library_id, material.material_id) not in index.materials:
                raise AgentPlanningError("material/library IDs are not owned by the reported context")

    if isinstance(action, SaveAsAction) and action.data_folder_id not in index.folder_ids:
        raise AgentPlanningError("data_folder_id is not owned by the reported cloud context")

    if isinstance(action, ExportAction):
        options = action.options
        if options.component_id:
            index.require_component(options.component_id)
        if options.body_id:
            index.require_semantic(options.body_id, {"body"}, options.component_id)
        if options.sketch_id:
            index.require_semantic(options.sketch_id, {"sketch"}, options.component_id)


_SYSTEM_PROMPT = """You are the bounded Autodesk Fusion CAD planning service.
Return exactly one JSON object and no markdown. It must have one of these shapes:
1. {"status":"proposed","action":<one CadAction>,"reason":<optional text>}
2. {"status":"needs_clarification","question":<text>}
3. {"status":"no_action","reason":<text>}
Only use the action names and persistent IDs supplied in capabilities/context. Never
invent an ID. Never output code, source, module, command, callable, method, function,
attribute path, or more than one action. If context is insufficient, request
clarification. Request IDs, connector IDs, preview mode and approval fields are
server-owned and will be replaced. Follow cad_action_json_schema exactly. For
every DimensionValue use exactly one representation: either {"expression":<text>}
or {"amount":<number>,"unit":<text>}; never combine the two representations."""


class AgentPlanner:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        plan_ttl_s: int = 300,
        now: Callable[[], datetime] | None = None,
    ):
        if not model.strip():
            raise AgentPlanningError("Fusion Agent LLM model is not configured", code="AGENT_NOT_CONFIGURED", http_status=503)
        self._client = client
        self._model = model
        self._plan_ttl_s = max(30, min(int(plan_ttl_s), 3_600))
        self._now = now or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_settings(cls, configured: Settings = settings) -> "AgentPlanner":
        try:
            client = make_llm_client()
        except RuntimeError as exc:
            raise AgentPlanningError(str(exc), code="AGENT_NOT_CONFIGURED", http_status=503) from exc
        return cls(
            client=client,
            model=configured.fusion_agent_model or configured.llm_model,
            plan_ttl_s=configured.fusion_agent_plan_ttl_s,
        )

    async def plan(self, turn: AgentTurnRequest) -> AgentPlanResponse:
        if turn.context.truncated:
            return self._non_action(
                turn,
                status="needs_clarification",
                question="Fusion context is truncated. Narrow the design scope and retry with a complete context.",
            )
        if turn.context.document is None:
            return self._non_action(
                turn,
                status="needs_clarification",
                question="Open a Fusion design document and retry.",
            )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "prompt": turn.prompt,
                        "capabilities": {
                            "actions": turn.capabilities.actions,
                            "context_sections": turn.capabilities.context_sections,
                        },
                        "cad_action_json_schema": CAD_ACTION_ADAPTER.json_schema(),
                        "context": turn.context.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]
        try:
            response = await self._client.chat.completions.create(
                stream=True,
                model=self._model,
                messages=messages,

                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
        except AgentPlanningError:
            raise
        except Exception as exc:
            raise AgentPlanningError(
                "Fusion Agent model request failed", code="AGENT_MODEL_UNAVAILABLE", http_status=503
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise AgentPlanningError("Fusion Agent model returned no JSON content")
        try:
            raw = json.loads(content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AgentPlanningError("Fusion Agent model response is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise AgentPlanningError("Fusion Agent model JSON must be an object")
        _reject_executable_shape(raw)
        try:
            model_plan = _ModelPlan.model_validate(raw)
        except ValidationError as exc:
            raise AgentPlanningError("Fusion Agent model JSON does not match the single-action contract") from exc

        if model_plan.status != "proposed":
            return self._non_action(
                turn,
                status=model_plan.status,
                question=model_plan.question,
                reason=model_plan.reason,
            )

        action_payload = dict(model_plan.action or {})
        action_payload.update(
            {
                "request_id": str(turn.request_id),
                "connector_instance_id": str(turn.connector_instance_id),
                "execution_mode": "preview",
                "approval_id": None,
                "idempotency_key": None,
            }
        )
        try:
            action = CAD_ACTION_ADAPTER.validate_python(action_payload)
        except ValidationError as exc:
            raise AgentPlanningError("Fusion Agent proposed an invalid or unsupported typed action") from exc
        if action.action not in turn.capabilities.actions:
            raise AgentPlanningError("Fusion Agent proposed an action not declared by this connector")
        validate_action_against_context(action, turn)
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return AgentPlanResponse(
            request_id=turn.request_id,
            proposal_id=uuid.uuid4(),
            connector_instance_id=turn.connector_instance_id,
            status="proposed",
            context_fingerprint=turn.context_fingerprint,
            action=action,
            risk=classify_risk(action.action),
            reason=model_plan.reason,
            expires_at=now + timedelta(seconds=self._plan_ttl_s),
        )

    @staticmethod
    def _non_action(
        turn: AgentTurnRequest,
        *,
        status: str,
        question: str | None = None,
        reason: str | None = None,
    ) -> AgentPlanResponse:
        return AgentPlanResponse(
            request_id=turn.request_id,
            proposal_id=uuid.uuid4(),
            connector_instance_id=turn.connector_instance_id,
            status=status,
            context_fingerprint=turn.context_fingerprint,
            question=question,
            reason=reason,
        )
