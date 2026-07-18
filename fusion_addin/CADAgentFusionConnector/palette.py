"""Main-thread Palette controller for direct Cloud Agent operation.

The browser is treated as untrusted input.  It can submit natural-language
intent or return an opaque nonce; it can never provide or alter a CadAction.
All context reads, native previews, mutations, and Palette output calls happen
on the thread that constructs :class:`PaletteController`.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Literal

from .config import ConnectorConfig
from .dispatcher import CancelToken


CONTRACT_VERSION = "1.0.0"
CONTEXT_FINGERPRINT_SCHEME = "ctx-c14n-1"
INTENT_SCHEME = "CAD-C14N-1"
MAX_PALETTE_DATA_BYTES = 16 * 1024
MAX_PROMPT_CHARS = 4000
CONTEXT_SECTIONS = [
    "application", "document", "design", "components", "occurrences",
    "selection", "parameters", "materials", "mass_properties", "sketches",
    "features", "timeline", "bodies", "assembly", "cloud",
]
RISK_BY_ACTION = {
    "cad.update_parameter": "medium",
    "cad.update_feature_parameter": "medium",
    "cad.create_sketch": "medium",
    "cad.create_extrude": "medium",
    "cad.create_hole": "medium",
    "cad.create_fillet": "medium",
    "cad.create_chamfer": "medium",
    "cad.update_entity_properties": "medium",
    "cad.save_document": "high",
    "cad.save_as": "high",
    "cad.export": "low",
}
_COMMON_ACTION_FIELDS = {
    "action", "request_id", "idempotency_key", "timeout_ms", "execution_mode",
    "approval_id", "connector_instance_id",
}
_ACTION_FIELDS = {
    "cad.update_parameter": {"target", "value"},
    "cad.update_feature_parameter": {"target", "value"},
    "cad.create_sketch": {"target", "plane", "unit", "name", "primitives"},
    "cad.create_extrude": {
        "target", "profile_id", "operation", "distance", "direction", "participant_body_ids",
    },
    "cad.create_hole": {"target", "sketch_point_ids", "diameter", "extent"},
    "cad.create_fillet": {"target", "edge_ids", "radius", "tangent_chain"},
    "cad.create_chamfer": {"target", "edge_ids", "distance", "tangent_chain"},
    "cad.update_entity_properties": {"target", "properties"},
    "cad.save_document": {"target", "version_description"},
    "cad.save_as": {"target", "data_folder_id", "name", "description", "tag"},
    "cad.export": {"target", "format", "filename", "options"},
}
_REQUEST_ONLY_FIELDS = {
    "request_id", "idempotency_key", "timeout_ms", "execution_mode",
    "approval_id", "connector_instance_id",
}
_PLAN_FIELDS = {
    "contract_version", "request_id", "proposal_id", "connector_instance_id", "status",
    "context_fingerprint", "action", "risk", "question", "reason", "expires_at",
}


class ControllerError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "category": "approval" if self.code.startswith("APPROVAL") else "validation",
            "retryable": False,
            "details": self.details,
        }


def _strict_object(value: Any, fields: set[str], message: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ControllerError("INVALID_AGENT_PLAN", message)
    return value


def parse_palette_event(event: str, raw_data: str) -> dict[str, Any]:
    if not isinstance(raw_data, str) or len(raw_data.encode("utf-8")) > MAX_PALETTE_DATA_BYTES:
        raise ControllerError("INVALID_PALETTE_EVENT", "Palette message exceeds its size limit")
    try:
        data = json.loads(raw_data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ControllerError("INVALID_PALETTE_EVENT", "Palette message must be valid JSON") from exc
    if not isinstance(data, dict):
        raise ControllerError("INVALID_PALETTE_EVENT", "Palette message must be an object")
    if event == "request_plan":
        allowed = {"prompt", "export_artifact_upload_consent", "f3d_upload_authorized"}
        if set(data) not in ({"prompt"}, allowed) or not isinstance(data.get("prompt"), str):
            raise ControllerError("INVALID_PALETTE_EVENT", "Planning fields are invalid")
        prompt = data["prompt"].strip()
        if not prompt or len(prompt) > MAX_PROMPT_CHARS:
            raise ControllerError("INVALID_PALETTE_EVENT", "Prompt length is invalid")
        upload = data.get("export_artifact_upload_consent", False)
        f3d = data.get("f3d_upload_authorized", False)
        if not isinstance(upload, bool) or not isinstance(f3d, bool) or (f3d and not upload):
            raise ControllerError("INVALID_PALETTE_EVENT", "Artifact upload consent is invalid")
        return {
            "prompt": prompt,
            "export_artifact_upload_consent": upload,
            "f3d_upload_authorized": f3d,
        }
    if event in {"approve", "approve_f3d_upload"}:
        if set(data) != {"approval_nonce"} or not isinstance(data.get("approval_nonce"), str):
            raise ControllerError("INVALID_PALETTE_EVENT", "Approval accepts only an opaque nonce")
        nonce = data["approval_nonce"]
        if not nonce or len(nonce) > 256:
            raise ControllerError("INVALID_PALETTE_EVENT", "Approval nonce is invalid")
        return {"approval_nonce": nonce}
    if event == "cancel" and data == {}:
        return {}
    raise ControllerError("INVALID_PALETTE_EVENT", "Unsupported Palette event")


def _canonical_number(value: int | float | Decimal) -> str:
    if isinstance(value, bool):
        raise TypeError("boolean is not a number")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite number")
    decimal = Decimal(str(value))
    if not decimal.is_finite():
        raise ValueError("non-finite number")
    if decimal == 0:
        return "0"
    rendered = format(decimal, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def canonical_json(value: Any) -> bytes:
    """Encode the dependency-free CAD-C14N-1 subset used by the Add-in."""

    def encode(item: Any) -> str:
        if item is None:
            return "null"
        if item is True:
            return "true"
        if item is False:
            return "false"
        if isinstance(item, (int, float, Decimal)):
            return _canonical_number(item)
        if isinstance(item, str):
            return json.dumps(unicodedata.normalize("NFC", item), ensure_ascii=False, separators=(",", ":"))
        if isinstance(item, (list, tuple)):
            return "[" + ",".join(encode(value) for value in item) + "]"
        if isinstance(item, dict):
            normalized: dict[str, Any] = {}
            for key, value in item.items():
                if not isinstance(key, str):
                    raise TypeError("canonical object key must be a string")
                normalized_key = unicodedata.normalize("NFC", key)
                if normalized_key in normalized:
                    raise ValueError("canonical object contains duplicate normalized keys")
                normalized[normalized_key] = value
            return "{" + ",".join(
                f"{encode(key)}:{encode(normalized[key])}" for key in sorted(normalized)
            ) + "}"
        raise TypeError("unsupported canonical value")

    return encode(value).encode("utf-8")


def _selected(item: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    source = item if isinstance(item, dict) else {}
    return {field: source.get(field) for field in fields}


def context_fingerprint_payload(context: dict[str, Any]) -> dict[str, Any]:
    raw_document = context.get("document")
    document = None if raw_document is None else _selected(
        raw_document, ("document_id", "is_saved", "is_modified", "is_read_only")
    )
    raw_cloud = context.get("cloud")
    cloud = None if raw_cloud is None else _selected(
        raw_cloud,
        (
            "data_file_id", "version_id", "version_number", "project_id",
            "folder_id", "is_complete", "is_read_only",
        ),
    )
    selection = sorted(
        (
            _selected(item, ("id", "kind", "component_id", "health_state"))
            for item in context.get("selection", []) if isinstance(item, dict)
        ),
        key=lambda item: (str(item["id"]), str(item["component_id"] or "")),
    )
    features = sorted(
        (
            _selected(item, ("id", "kind", "component_id", "health_state"))
            for item in context.get("features", []) if isinstance(item, dict)
        ),
        key=lambda item: (str(item["id"]), str(item["component_id"])),
    )
    parameters = sorted(
        (
            _selected(item, ("id", "component_id", "created_by_id", "expression"))
            for item in context.get("parameters", []) if isinstance(item, dict)
        ),
        key=lambda item: (str(item["id"]), str(item["component_id"])),
    )
    timeline = sorted(
        (
            {
                "index": item.get("index"),
                "name": item.get("name"),
                "kind": item.get("kind"),
                "entity_id": item.get("entity_id"),
                "health_state": item.get("health_state"),
                "is_group": item.get("is_group", False),
                "is_rolled_back": item.get("is_rolled_back", False),
                "is_suppressed": item.get("is_suppressed"),
                "parent": None if item.get("parent") is None else _selected(item["parent"], ("index", "name")),
                "error_or_warning": item.get("error_or_warning"),
            }
            for item in context.get("timeline", []) if isinstance(item, dict)
        ),
        key=lambda item: (
            int(item["index"]) if isinstance(item["index"], int) else -1,
            str(item["name"]), str(item["entity_id"] or ""),
        ),
    )
    return {
        "version": "1",
        "document": document,
        "cloud": cloud,
        "selection": selection,
        "features": features,
        "parameters": parameters,
        "timeline": timeline,
    }


def context_fingerprint(context: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(context_fingerprint_payload(context))).hexdigest()
    return f"{CONTEXT_FINGERPRINT_SCHEME}:{digest}"


def cloud_context(context: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded Agent context with local user identity removed."""

    try:
        sanitized = json.loads(
            json.dumps(context, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
        )
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ControllerError("CONTEXT_READ_FAILED", "Fusion context is not finite JSON") from exc
    application = sanitized.get("application")
    if isinstance(application, dict) and "user_name" in application:
        application["user_name"] = None
    return sanitized


def action_intent_hash(action: dict[str, Any]) -> str:
    intent = {key: value for key, value in action.items() if key not in _REQUEST_ONLY_FIELDS}
    return hashlib.sha256(canonical_json(intent)).hexdigest()


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    """Remove executor-only evidence before Cloud Agent or Palette output."""

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: clean(item)
                for key, item in value.items()
                if isinstance(key, str) and not key.startswith("_")
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(result)


@dataclass(frozen=True)
class ApprovalBinding:
    credential_subject: str
    connector_instance_id: str
    document_id: str
    intent_scheme: str
    intent_hash: str
    context_fingerprint: str
    proposal_id: str
    preview_id: str
    request_id: str
    risk: Literal["low", "medium", "high"]
    purpose: Literal["execute", "f3d_upload"]
    expires_at: float


class ApprovalLedger:
    """In-memory, atomic, one-time approvals; restart invalidates all grants."""

    def __init__(self, *, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, ApprovalBinding] = {}

    def issue(self, binding: ApprovalBinding) -> str:
        if binding.intent_scheme != INTENT_SCHEME or binding.expires_at <= self._clock():
            raise ControllerError("APPROVAL_INVALID", "Approval binding is invalid")
        nonce = secrets.token_urlsafe(32)
        with self._lock:
            self._entries[nonce] = binding
        return nonce

    def consume(self, nonce: str, expected: ApprovalBinding) -> ApprovalBinding:
        with self._lock:
            binding = self._entries.pop(nonce, None)
        if binding is None:
            raise ControllerError("APPROVAL_INVALID", "Approval is invalid or already used")
        if self._clock() >= binding.expires_at:
            raise ControllerError("APPROVAL_EXPIRED", "Approval has expired")
        if binding != expected:
            raise ControllerError("APPROVAL_INVALID", "Approval binding changed and was invalidated")
        return binding

    def invalidate_all(self) -> None:
        with self._lock:
            self._entries.clear()


def _uuid(value: Any, message: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ControllerError("INVALID_AGENT_PLAN", message) from exc


def _parse_expiry(value: Any) -> float:
    if not isinstance(value, str):
        raise ControllerError("INVALID_AGENT_PLAN", "Agent plan expiry is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControllerError("INVALID_AGENT_PLAN", "Agent plan expiry is invalid") from exc
    if parsed.tzinfo is None:
        raise ControllerError("INVALID_AGENT_PLAN", "Agent plan expiry must include a timezone")
    return parsed.timestamp()


def _keys(value: Any, expected: set[str], message: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ControllerError("INVALID_AGENT_PLAN", message)
    return value


def _text(value: Any, *, optional: bool = False, maximum: int = 4096) -> bool:
    return (optional and value is None) or (isinstance(value, str) and 0 < len(value) <= maximum)


def _dimension(value: Any) -> None:
    item = _keys(value, {"amount", "unit", "expression"}, "Agent dimension fields are invalid")
    expression = item["expression"]
    dimensional = item["amount"] is not None or item["unit"] is not None
    if dimensional == (expression is not None):
        raise ControllerError("INVALID_AGENT_PLAN", "Agent dimension must use exactly one value form")
    if expression is not None:
        if not _text(expression, maximum=256):
            raise ControllerError("INVALID_AGENT_PLAN", "Agent dimension expression is invalid")
    elif (
        isinstance(item["amount"], bool)
        or not isinstance(item["amount"], (int, float))
        or not math.isfinite(float(item["amount"]))
        or not _text(item["unit"], maximum=32)
    ):
        raise ControllerError("INVALID_AGENT_PLAN", "Agent dimensional amount/unit is invalid")


def _point(value: Any) -> None:
    point = _keys(value, {"x", "y"}, "Agent sketch point fields are invalid")
    if any(isinstance(point[key], bool) or not isinstance(point[key], (int, float)) or not math.isfinite(float(point[key])) for key in ("x", "y")):
        raise ControllerError("INVALID_AGENT_PLAN", "Agent sketch point is invalid")


def _validate_action_shape(name: str, action: dict[str, Any]) -> None:
    target = action["target"]
    document_target = {"document_id"}
    component_target = document_target | {"component_id"}
    parameter_target = component_target | {"parameter_id"}
    if name == "cad.update_parameter":
        _keys(target, parameter_target, "Parameter target fields are invalid")
        _dimension(action["value"])
    elif name == "cad.update_feature_parameter":
        _keys(target, parameter_target | {"feature_id"}, "Feature parameter target fields are invalid")
        _dimension(action["value"])
    elif name == "cad.create_sketch":
        _keys(target, component_target, "Sketch target fields are invalid")
        plane = action["plane"]
        if not isinstance(plane, dict) or plane.get("kind") not in {"origin", "entity"}:
            raise ControllerError("INVALID_AGENT_PLAN", "Sketch plane is invalid")
        _keys(
            plane,
            {"kind", "plane"} if plane["kind"] == "origin" else {"kind", "entity_id"},
            "Sketch plane fields are invalid",
        )
        if plane["kind"] == "origin" and plane["plane"] not in {"xy", "xz", "yz"}:
            raise ControllerError("INVALID_AGENT_PLAN", "Sketch origin plane is invalid")
        if action["unit"] not in {"mm", "cm", "m", "in", "ft"} or not _text(action["name"], optional=True, maximum=255):
            raise ControllerError("INVALID_AGENT_PLAN", "Sketch metadata is invalid")
        primitives = action["primitives"]
        if not isinstance(primitives, list) or not 1 <= len(primitives) <= 500:
            raise ControllerError("INVALID_AGENT_PLAN", "Sketch primitive count is invalid")
        for primitive in primitives:
            if not isinstance(primitive, dict):
                raise ControllerError("INVALID_AGENT_PLAN", "Sketch primitive is invalid")
            kind = primitive.get("kind")
            fields = {
                "line": {"kind", "start", "end"},
                "circle": {"kind", "center", "radius"},
                "rectangle": {"kind", "corner1", "corner2"},
            }.get(kind)
            if fields is None:
                raise ControllerError("INVALID_AGENT_PLAN", "Sketch primitive kind is invalid")
            _keys(primitive, fields, "Sketch primitive fields are invalid")
            if kind == "line":
                _point(primitive["start"]); _point(primitive["end"])
            elif kind == "rectangle":
                _point(primitive["corner1"]); _point(primitive["corner2"])
            else:
                _point(primitive["center"])
                radius = primitive["radius"]
                if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not math.isfinite(float(radius)) or radius <= 0:
                    raise ControllerError("INVALID_AGENT_PLAN", "Sketch circle radius is invalid")
    elif name == "cad.create_extrude":
        _keys(target, component_target, "Extrude target fields are invalid")
        _dimension(action["distance"])
        if action["operation"] not in {"new_body", "new_component", "join", "cut", "intersect"} or action["direction"] not in {"positive", "negative", "symmetric"}:
            raise ControllerError("INVALID_AGENT_PLAN", "Extrude operation is invalid")
        if not _text(action["profile_id"]) or not isinstance(action["participant_body_ids"], list) or len(action["participant_body_ids"]) > 500:
            raise ControllerError("INVALID_AGENT_PLAN", "Extrude entity list is invalid")
        if action["participant_body_ids"] and action["operation"] not in {"join", "cut", "intersect"}:
            raise ControllerError("INVALID_AGENT_PLAN", "Extrude participants do not apply to this operation")
    elif name == "cad.create_hole":
        _keys(target, component_target, "Hole target fields are invalid")
        points = action["sketch_point_ids"]
        if not isinstance(points, list) or not 1 <= len(points) <= 100 or not all(_text(value) for value in points):
            raise ControllerError("INVALID_AGENT_PLAN", "Hole sketch point IDs are invalid")
        _dimension(action["diameter"])
        extent = action["extent"]
        if not isinstance(extent, dict) or extent.get("kind") not in {"distance", "through_all"}:
            raise ControllerError("INVALID_AGENT_PLAN", "Hole extent is invalid")
        _keys(extent, {"kind", "distance"} if extent["kind"] == "distance" else {"kind", "direction"}, "Hole extent fields are invalid")
        if extent["kind"] == "distance":
            _dimension(extent["distance"])
        elif extent["direction"] not in {"positive", "negative"}:
            raise ControllerError("INVALID_AGENT_PLAN", "Hole direction is invalid")
    elif name in {"cad.create_fillet", "cad.create_chamfer"}:
        _keys(target, component_target, "Edge feature target fields are invalid")
        edges = action["edge_ids"]
        if not isinstance(edges, list) or not 1 <= len(edges) <= 500 or not all(_text(value) for value in edges):
            raise ControllerError("INVALID_AGENT_PLAN", "Edge IDs are invalid")
        _dimension(action["radius"] if name == "cad.create_fillet" else action["distance"])
        if not isinstance(action["tangent_chain"], bool):
            raise ControllerError("INVALID_AGENT_PLAN", "Tangent-chain option is invalid")
    elif name == "cad.update_entity_properties":
        _keys(target, {"document_id", "component_id", "entity_id", "entity_kind"}, "Entity target fields are invalid")
        if target["entity_kind"] not in {"component", "occurrence", "body"}:
            raise ControllerError("INVALID_AGENT_PLAN", "Entity kind is invalid")
        properties = _keys(action["properties"], {"name", "part_number", "description", "material"}, "Entity property fields are invalid")
        if all(value is None for value in properties.values()):
            raise ControllerError("INVALID_AGENT_PLAN", "At least one entity property is required")
        if properties["material"] is not None:
            material = _keys(properties["material"], {"library_id", "material_id"}, "Material reference fields are invalid")
            if not all(_text(value, maximum=1024) for value in material.values()):
                raise ControllerError("INVALID_AGENT_PLAN", "Material reference is invalid")
    elif name == "cad.save_document":
        _keys(target, document_target, "Save target fields are invalid")
        if not isinstance(action["version_description"], str) or len(action["version_description"]) > 1024:
            raise ControllerError("INVALID_AGENT_PLAN", "Save description is invalid")
    elif name == "cad.save_as":
        _keys(target, document_target, "Save-as target fields are invalid")
        if not _text(action["data_folder_id"], maximum=2048) or not _text(action["name"], maximum=255):
            raise ControllerError("INVALID_AGENT_PLAN", "Save-as destination is invalid")
        if not isinstance(action["description"], str) or len(action["description"]) > 1024 or not isinstance(action["tag"], str) or len(action["tag"]) > 255:
            raise ControllerError("INVALID_AGENT_PLAN", "Save-as metadata is invalid")
    elif name == "cad.export":
        _keys(target, document_target, "Export target fields are invalid")
        options = _keys(
            action["options"],
            {"component_id", "body_id", "sketch_id", "mesh_refinement", "binary", "width", "height"},
            "Export option fields are invalid",
        )
        if action["format"] not in {"step", "stl", "dxf", "f3d", "png"}:
            raise ControllerError("INVALID_AGENT_PLAN", "Export format is invalid")
        if not _text(action["filename"], maximum=255) or Path(action["filename"]).name != action["filename"]:
            raise ControllerError("INVALID_AGENT_PLAN", "Export filename is invalid")


def _validate_agent_action(
    raw: Any,
    *,
    turn_request_id: str,
    connector_instance_id: str,
    document_id: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ControllerError("INVALID_AGENT_PLAN", "Agent proposal must contain one action object")
    name = raw.get("action")
    specific = _ACTION_FIELDS.get(name)
    if specific is None or set(raw) != _COMMON_ACTION_FIELDS | specific:
        raise ControllerError("INVALID_AGENT_PLAN", "Agent action or fields are not allowlisted")
    if _uuid(raw.get("request_id"), "Agent action request ID is invalid") != turn_request_id:
        raise ControllerError("INVALID_AGENT_PLAN", "Agent action request ID does not match the turn")
    if _uuid(raw.get("connector_instance_id"), "Agent connector ID is invalid") != connector_instance_id:
        raise ControllerError("INVALID_AGENT_PLAN", "Agent action connector does not match this Add-in")
    if raw.get("execution_mode") != "preview" or raw.get("approval_id") is not None or raw.get("idempotency_key") is not None:
        raise ControllerError("INVALID_AGENT_PLAN", "Agent cannot bypass native preview or local approval")
    if not isinstance(raw.get("timeout_ms"), int) or not 1000 <= raw["timeout_ms"] <= 300000:
        raise ControllerError("INVALID_AGENT_PLAN", "Agent action timeout is invalid")
    target = raw.get("target")
    _validate_action_shape(str(name), raw)
    if not isinstance(target, dict) or target.get("document_id") != document_id:
        raise ControllerError("INVALID_AGENT_PLAN", "Agent action targets another document")
    components = {
        str(item.get("id")): item
        for item in context.get("components", [])
        if isinstance(item, dict) and item.get("id") is not None
    }
    parameters = {
        str(item.get("id")): item
        for item in context.get("parameters", [])
        if isinstance(item, dict) and item.get("id") is not None
    }
    semantic: dict[str, list[tuple[str, str | None]]] = {}

    def add_semantic(entity_id: Any, kind: str, owner_component_id: Any) -> None:
        if not isinstance(entity_id, str) or not entity_id:
            return
        ref = (kind.strip().lower().replace("-", "_").replace(" ", "_"),
               str(owner_component_id) if owner_component_id is not None else None)
        if ref not in semantic.setdefault(entity_id, []):
            semantic[entity_id].append(ref)

    collection_kinds = {
        "components": "component", "occurrences": "occurrence", "sketches": "sketch",
        "features": "feature", "bodies": "body", "assembly": "assembly",
    }
    entities: dict[str, dict[str, Any]] = {}
    for field, semantic_kind in collection_kinds.items():
        for item in context.get(field, []):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            entity_id = item["id"]
            entities[entity_id] = item
            owner = entity_id if semantic_kind == "component" else item.get("component_id")
            add_semantic(entity_id, semantic_kind, owner)
    for item in context.get("selection", []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        entities[item["id"]] = item
        add_semantic(item["id"], str(item.get("kind", "selection")), item.get("component_id"))
    for entity_id, item in entities.items():
        extensions = item.get("vendor_extensions")
        if not isinstance(extensions, dict):
            continue
        for key, semantic_kind in (
            ("profile_ids", "profile"), ("sketch_point_ids", "sketch_point"),
            ("edge_ids", "edge"), ("face_ids", "face"), ("plane_ids", "plane"),
        ):
            values = extensions.get(key, [])
            if isinstance(values, list):
                for value in values:
                    add_semantic(value, semantic_kind, item.get("component_id"))

    def require_semantic(entity_id: Any, kinds: set[str], owner_component_id: Any = None) -> None:
        normalized_id = str(entity_id)
        matches = [ref for ref in semantic.get(normalized_id, []) if ref[0] in kinds]
        if not matches:
            raise ControllerError(
                "INVALID_AGENT_PLAN", "Agent entity is absent with the required semantic kind"
            )
        if owner_component_id is not None and not any(
            ref[1] == str(owner_component_id) for ref in matches
        ):
            raise ControllerError(
                "INVALID_AGENT_PLAN", "Agent entity does not belong to the target component"
            )

    component_id = target.get("component_id")
    if component_id is not None and str(component_id) not in components:
        raise ControllerError("INVALID_AGENT_PLAN", "Agent target component is absent from context")
    parameter_id = target.get("parameter_id")
    if parameter_id is not None:
        parameter = parameters.get(str(parameter_id))
        if parameter is None or (
            parameter.get("component_id") is not None
            and component_id is not None
            and parameter.get("component_id") != component_id
        ):
            raise ControllerError("INVALID_AGENT_PLAN", "Agent target parameter ownership is invalid")
    entity_id = target.get("entity_id")
    if name == "cad.update_feature_parameter":
        feature_id = str(target.get("feature_id"))
        require_semantic(feature_id, {"feature"}, component_id)
        if parameters[str(parameter_id)].get("created_by_id") != feature_id:
            raise ControllerError("INVALID_AGENT_PLAN", "Agent feature parameter ownership is invalid")
    if name == "cad.create_sketch" and raw["plane"].get("kind") == "entity":
        require_semantic(
            raw["plane"]["entity_id"],
            {"plane", "face", "brepface", "construction_plane", "constructionplane"},
            component_id,
        )
    if name == "cad.create_extrude":
        require_semantic(raw["profile_id"], {"profile"}, component_id)
        for body_id in raw["participant_body_ids"]:
            require_semantic(body_id, {"body"}, component_id)
    if name == "cad.create_hole":
        for point_id in raw["sketch_point_ids"]:
            require_semantic(point_id, {"sketch_point"}, component_id)
    if name in {"cad.create_fillet", "cad.create_chamfer"}:
        for edge_id in raw["edge_ids"]:
            require_semantic(edge_id, {"edge"}, component_id)
    if name == "cad.update_entity_properties":
        require_semantic(entity_id, {str(target["entity_kind"])}, component_id)
    if name == "cad.update_entity_properties" and raw["properties"].get("material") is not None:
        material_ref = raw["properties"]["material"]
        if not any(
            isinstance(item, dict)
            and item.get("id") == material_ref["material_id"]
            and item.get("library_id") == material_ref["library_id"]
            for item in context.get("materials", [])
        ):
            raise ControllerError("INVALID_AGENT_PLAN", "Agent material reference is absent from context")
    if name == "cad.save_as":
        cloud = context.get("cloud") if isinstance(context.get("cloud"), dict) else {}
        if raw["data_folder_id"] != cloud.get("folder_id"):
            raise ControllerError("INVALID_AGENT_PLAN", "Agent save-as folder is absent from context")
    if name == "cad.export":
        options = raw["options"]
        if options.get("component_id") is not None and str(options["component_id"]) not in components:
            raise ControllerError("INVALID_AGENT_PLAN", "Agent export component is absent from context")
        if options.get("body_id") is not None:
            require_semantic(options["body_id"], {"body"}, options.get("component_id"))
        if options.get("sketch_id") is not None:
            require_semantic(options["sketch_id"], {"sketch"}, options.get("component_id"))
    # Ensure nested values are finite JSON and within the direct transport cap.
    try:
        encoded = json.dumps(raw, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ControllerError("INVALID_AGENT_PLAN", "Agent action is not finite JSON") from exc
    if len(encoded) > MAX_PALETTE_DATA_BYTES * 8:
        raise ControllerError("INVALID_AGENT_PLAN", "Agent action is too large")
    return json.loads(encoded.decode("utf-8"))


class PaletteController:
    def __init__(
        self,
        config: ConnectorConfig,
        dispatcher: Any,
        transport: Any,
        send_to_palette: Callable[[str, str], None],
        *,
        capabilities: dict[str, Any] | None = None,
        confirm_high_risk: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.dispatcher = dispatcher
        self.transport = transport
        self.send_to_palette = send_to_palette
        self.capabilities = capabilities or {
            "adapter": "fusion360", "contract_version": CONTRACT_VERSION,
            "protocol_versions": [1], "available": True, "runtime_online": False,
            "connector_online": True, "actions": sorted(RISK_BY_ACTION),
            "context_sections": list(CONTEXT_SECTIONS), "limitations": [],
        }
        self.confirm_high_risk = confirm_high_risk or (lambda _message: False)
        self.clock = clock
        self.main_thread_id = threading.get_ident()
        self.approvals = ApprovalLedger(clock=clock)
        self.pending: dict[str, Any] | None = None
        self.current_turn: dict[str, Any] | None = None
        self.last_result: dict[str, Any] | None = None
        self.uploaded_artifacts: list[dict[str, Any]] = []
        self.obsolete_request_ids: list[str] = []
        self.stopped = False

    def handle_palette_event(self, event: str, raw_data: str) -> None:
        self._assert_main_thread()
        if self.stopped:
            raise ControllerError("CONTROLLER_STOPPED", "Palette controller is stopped")
        try:
            data = parse_palette_event(event, raw_data)
            if event == "request_plan":
                self._request_plan(
                    data["prompt"],
                    export_upload=data["export_artifact_upload_consent"],
                    f3d_upload=data["f3d_upload_authorized"],
                )
            elif event == "approve":
                self._approve(data["approval_nonce"])
            elif event == "approve_f3d_upload":
                self._approve_f3d(data["approval_nonce"])
            elif event == "cancel":
                if self.current_turn is not None:
                    self._mark_obsolete(str(self.current_turn["request_id"]))
                self.approvals.invalidate_all()
                self.pending = None
                self.current_turn = None
                self._send_state("idle", message="Request cancelled locally")
        except ControllerError as exc:
            self._send_error(exc)
            raise

    def handle_worker_message(self, message: dict[str, Any]) -> None:
        self._assert_main_thread()
        if self.stopped:
            return
        try:
            if not isinstance(message, dict):
                raise ControllerError("INVALID_AGENT_PLAN", "Worker response is invalid")
            message_request_id = str(message.get("request_id", ""))
            if message_request_id in self.obsolete_request_ids:
                return
            if message.get("kind") == "transport_error":
                if self.current_turn is not None and message_request_id != self.current_turn["request_id"]:
                    return
                error = message.get("error") if isinstance(message.get("error"), dict) else {}
                operation = message.get("operation")
                if operation == "report" and self.last_result is not None:
                    self.last_result = {
                        **self.last_result,
                        "status": "indeterminate",
                        "warnings": list(self.last_result.get("warnings", [])) + [
                            "The verified local result is pending Cloud Agent report reconciliation"
                        ],
                        "error": {
                            "code": "AGENT_REPORT_UNCONFIRMED",
                            "message": "Fusion changed locally but the Cloud Agent report is not confirmed",
                            "category": "transport",
                            "retryable": True,
                            "details": {},
                        },
                    }
                    self._send_state("indeterminate", result=self.last_result, error=error)
                    return
                if operation == "artifact" and self.last_result is not None:
                    self.last_result = {
                        **self.last_result,
                        "warnings": list(self.last_result.get("warnings", [])) + [
                            "The export remains local because its authorized upload failed"
                        ],
                    }
                    self._send_state(
                        str(self.last_result.get("status", "indeterminate")),
                        result=self.last_result,
                        error=error,
                    )
                    return
                raise ControllerError(
                    str(error.get("code", "NETWORK_UNAVAILABLE")),
                    str(error.get("message", "Cloud Agent request failed")),
                )
            if message.get("kind") == "plan_result":
                self._handle_plan(
                    message.get("request_id"),
                    message.get("payload"),
                    message.get("credential_subject"),
                )
                return
            if message.get("kind") == "report_result":
                payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                report_id = payload.get("report_id")
                should_reconcile_journal = (
                    self.last_result is None
                    or self.last_result.get("action") != "cad.export"
                )
                if report_id and should_reconcile_journal and hasattr(self.dispatcher, "journal"):
                    try:
                        self.dispatcher.journal.mark_agent_reported(message_request_id, str(report_id))
                    except Exception as exc:
                        raise ControllerError(
                            "REPORT_JOURNAL_ERROR", "Cloud report receipt could not be reconciled locally"
                        ) from exc
                return
            if message.get("kind") == "artifact_result":
                payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
                if payload and self.last_result is None:
                    raise ControllerError(
                        "ARTIFACT_RESULT_ORPHANED",
                        "Cloud artifact receipt has no verified local execution result",
                    )
                if payload:
                    self.uploaded_artifacts.append(_public_result(payload))
                    self._send_state(
                        str(self.last_result.get("status", "indeterminate")),
                        result=self.last_result,
                        uploaded_artifacts=list(self.uploaded_artifacts),
                    )
                return
            raise ControllerError("INVALID_AGENT_PLAN", "Worker response kind is not allowlisted")
        except ControllerError as exc:
            self.approvals.invalidate_all()
            self.pending = None
            self._send_error(exc)

    def stop(self) -> None:
        self._assert_main_thread()
        self.stopped = True
        self.approvals.invalidate_all()
        self.pending = None
        self.current_turn = None
        self.last_result = None
        self.uploaded_artifacts = []

    def _request_plan(self, prompt: str, *, export_upload: bool, f3d_upload: bool) -> None:
        if self.current_turn is not None:
            self._mark_obsolete(str(self.current_turn["request_id"]))
        self.approvals.invalidate_all()
        self.pending = None
        self.last_result = None
        self.uploaded_artifacts = []
        self._send_state("reading_context")
        context = cloud_context(self._capture_context())
        fingerprint = context_fingerprint(context)
        request_id = str(uuid.uuid4())
        turn = {
            "contract_version": CONTRACT_VERSION,
            "request_id": request_id,
            "connector_instance_id": self.config.connector_instance_id,
            "prompt": prompt,
            "context": context,
            "context_fingerprint": fingerprint,
            "capabilities": self.capabilities,
            "export_artifact_upload_consent": export_upload,
            "f3d_upload_authorized": f3d_upload,
        }
        encoded = json.dumps(turn, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.config.max_request_bytes:
            raise ControllerError("CONTEXT_TOO_LARGE", "Fusion context exceeds the configured upload limit")
        self.current_turn = turn
        if not self.transport.submit({"kind": "plan", "payload": turn}):
            self.current_turn = None
            raise ControllerError("TRANSPORT_QUEUE_FULL", "Cloud Agent queue is full")
        self._send_state("planning", request_id=request_id)

    def _handle_plan(self, request_id: Any, raw: Any, credential_subject: Any) -> None:
        turn = self.current_turn
        if turn is None or request_id != turn["request_id"]:
            raise ControllerError("INVALID_AGENT_PLAN", "Agent plan does not match the active turn")
        if not isinstance(raw, dict) or set(raw) != _PLAN_FIELDS:
            raise ControllerError("INVALID_AGENT_PLAN", "Agent plan fields are invalid")
        if raw["contract_version"] != CONTRACT_VERSION:
            raise ControllerError("INVALID_AGENT_PLAN", "Agent contract version is incompatible")
        if _uuid(raw["request_id"], "Agent request ID is invalid") != turn["request_id"]:
            raise ControllerError("INVALID_AGENT_PLAN", "Agent request ID does not match the active turn")
        if _uuid(raw["connector_instance_id"], "Agent connector ID is invalid") != self.config.connector_instance_id:
            raise ControllerError("INVALID_AGENT_PLAN", "Agent plan targets another connector")
        if raw["context_fingerprint"] != turn["context_fingerprint"]:
            raise ControllerError("STALE_CONTEXT", "Agent plan was generated from another Fusion context")
        status = raw.get("status")
        if status == "needs_clarification":
            if raw.get("action") is not None or not isinstance(raw.get("question"), str):
                raise ControllerError("INVALID_AGENT_PLAN", "Clarification response is invalid")
            self._send_state("idle", question=raw["question"])
            self.current_turn = None
            return
        if status == "no_action":
            if raw.get("action") is not None or not isinstance(raw.get("reason"), str):
                raise ControllerError("INVALID_AGENT_PLAN", "No-action response is invalid")
            self._send_state("idle", message=raw["reason"])
            self.current_turn = None
            return
        if status != "proposed":
            raise ControllerError("INVALID_AGENT_PLAN", "Agent plan status is invalid")
        if not isinstance(credential_subject, str) or not credential_subject.startswith("bearer-sha256:"):
            raise ControllerError("INVALID_AGENT_PLAN", "Authenticated credential identity is missing")
        proposal_id = _uuid(raw.get("proposal_id"), "Agent proposal ID is invalid")
        risk = raw.get("risk")
        if risk not in {"low", "medium", "high"}:
            raise ControllerError("INVALID_AGENT_PLAN", "Agent proposal risk is invalid")
        expires_at = _parse_expiry(raw.get("expires_at"))
        if expires_at <= self.clock():
            raise ControllerError("INVALID_AGENT_PLAN", "Agent proposal has expired")

        document_id = str(turn["context"]["document"]["document_id"])
        action = _validate_agent_action(
            raw.get("action"),
            turn_request_id=turn["request_id"],
            connector_instance_id=self.config.connector_instance_id,
            document_id=document_id,
            context=turn["context"],
        )
        if RISK_BY_ACTION[action["action"]] != risk:
            raise ControllerError("INVALID_AGENT_PLAN", "Agent action risk does not match local policy")

        self._send_state("native_preview")
        fresh_context = self._capture_context()
        fresh_fingerprint = context_fingerprint(fresh_context)
        if fresh_fingerprint != turn["context_fingerprint"]:
            raise ControllerError("STALE_CONTEXT", "Fusion context changed before native preview")
        _validate_agent_action(
            action,
            turn_request_id=turn["request_id"],
            connector_instance_id=self.config.connector_instance_id,
            document_id=document_id,
            context=fresh_context,
        )
        preview_id = str(uuid.uuid4())
        preview_action = {
            **action,
            "request_id": preview_id,
            "execution_mode": "preview",
            "approval_id": None,
        }
        preview = self.dispatcher.dispatch(
            self._task(preview_action, preview_id, action_intent_hash(action)), CancelToken()
        )
        if preview.get("status") != "success" or not isinstance(preview.get("data"), dict):
            raise ControllerError("NATIVE_PREVIEW_FAILED", "Fusion native preview failed")
        binding = ApprovalBinding(
            credential_subject=credential_subject,
            connector_instance_id=self.config.connector_instance_id,
            document_id=document_id,
            intent_scheme=INTENT_SCHEME,
            intent_hash=action_intent_hash(action),
            context_fingerprint=fresh_fingerprint,
            proposal_id=proposal_id,
            preview_id=preview_id,
            request_id=turn["request_id"],
            risk=risk,
            purpose="execute",
            expires_at=min(expires_at, self.clock() + self.config.approval_ttl_s),
        )
        nonce = self.approvals.issue(binding)
        f3d_binding = None
        f3d_nonce = None
        if (
            action["action"] == "cad.export"
            and action.get("format") == "f3d"
            and turn["export_artifact_upload_consent"]
            and turn["f3d_upload_authorized"]
        ):
            f3d_binding = ApprovalBinding(
                **{**asdict(binding), "purpose": "f3d_upload", "request_id": str(uuid.uuid4())}
            )
            f3d_nonce = self.approvals.issue(f3d_binding)
        self.pending = {
            "turn": turn, "credential_subject": credential_subject, "proposal": raw,
            "action": action, "preview": preview, "binding": binding,
            "approval_nonce": nonce, "f3d_binding": f3d_binding,
            "f3d_upload_authorized": False,
        }
        self._send_state(
            "awaiting_approval",
            proposal={
                "proposal_id": proposal_id, "risk": risk, "action": action,
                "reason": raw.get("reason"), "expires_at": raw.get("expires_at"),
            },
            preview=preview,
            approval_nonce=nonce,
            f3d_consent_nonce=f3d_nonce,
        )

    def _approve_f3d(self, nonce: str) -> None:
        pending = self.pending
        if not pending or pending.get("f3d_binding") is None:
            raise ControllerError("APPROVAL_INVALID", "No F3D upload is awaiting authorization")
        self.approvals.consume(nonce, pending["f3d_binding"])
        pending["f3d_upload_authorized"] = True
        self._send_state(
            "awaiting_approval",
            message="F3D upload separately authorized",
            approval_nonce=pending["approval_nonce"],
        )

    def _approve(self, nonce: str) -> None:
        pending = self.pending
        if pending is None:
            raise ControllerError("APPROVAL_INVALID", "No action is awaiting approval")
        binding: ApprovalBinding = pending["binding"]
        fresh_context = self._capture_context()
        if context_fingerprint(fresh_context) != binding.context_fingerprint:
            self.approvals.invalidate_all()
            self.pending = None
            raise ControllerError("STALE_CONTEXT", "Fusion context changed; approval was invalidated")
        if action_intent_hash(pending["action"]) != binding.intent_hash:
            self.approvals.invalidate_all()
            self.pending = None
            raise ControllerError("APPROVAL_INVALID", "Proposed action changed; approval was invalidated")
        _validate_agent_action(
            pending["action"],
            turn_request_id=binding.request_id,
            connector_instance_id=binding.connector_instance_id,
            document_id=binding.document_id,
            context=fresh_context,
        )
        self.approvals.consume(nonce, binding)
        if binding.risk == "high" and not self.confirm_high_risk(
            f"Confirm high-risk Fusion operation: {pending['action']['action']}"
        ):
            self.pending = None
            raise ControllerError("APPROVAL_REJECTED", "Fusion native confirmation was not granted")

        self._send_state("executing")
        execute_action = {
            **pending["action"],
            "request_id": binding.request_id,
            "execution_mode": "execute",
            "approval_id": None,
        }
        result = self.dispatcher.dispatch(
            self._task(execute_action, binding.request_id, binding.intent_hash), CancelToken()
        )
        report = {
            "contract_version": CONTRACT_VERSION,
            "report_id": str(uuid.uuid4()),
            "request_id": binding.request_id,
            "connector_instance_id": self.config.connector_instance_id,
            "proposal_id": binding.proposal_id,
            "context_fingerprint": binding.context_fingerprint,
            "action_intent_hash": binding.intent_hash,
            "result": _public_result(result),
            "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if pending["action"]["action"] != "cad.export" and hasattr(self.dispatcher, "journal"):
            try:
                self.dispatcher.journal.record_agent_report(binding.request_id, report)
            except Exception:
                result = {**result, "status": "indeterminate"}
                result["warnings"] = list(result.get("warnings", [])) + [
                    "Mutation completed but its Cloud Agent report could not be durably queued"
                ]
        if not self.transport.submit({"kind": "report", "payload": report}):
            # Execution has already happened and the durable journal owns replay
            # safety.  Never run the mutation again merely because reporting is
            # unavailable.
            result = {**result, "status": "indeterminate"}
            result["warnings"] = list(result.get("warnings", [])) + [
                "Mutation completed locally but its Agent report could not be queued"
            ]
        local_artifacts = result.get("_local_artifacts", [])
        turn = pending["turn"]
        if local_artifacts and turn["export_artifact_upload_consent"]:
            for artifact in local_artifacts:
                if not isinstance(artifact, dict):
                    continue
                kind = str(artifact.get("kind", ""))
                if kind == "f3d" and not (
                    turn["f3d_upload_authorized"] and pending["f3d_upload_authorized"]
                ):
                    result["warnings"] = list(result.get("warnings", [])) + [
                        "F3D remained local because its separate upload approval was not consumed"
                    ]
                    continue
                relative_path = artifact.get("relative_path")
                if not isinstance(relative_path, str) or Path(relative_path).name != relative_path:
                    result["warnings"] = list(result.get("warnings", [])) + [
                        "Invalid local artifact was not uploaded"
                    ]
                    continue
                consent = {
                    "request_id": binding.request_id,
                    "artifact_id": artifact.get("artifact_id"),
                    "filename": artifact.get("filename"),
                    "format": kind,
                    "size_bytes": artifact.get("size_bytes"),
                    "sha256": artifact.get("sha256"),
                    "upload_authorized": True,
                    "f3d_upload_authorized": kind == "f3d" and pending["f3d_upload_authorized"],
                }
                submitted = self.transport.submit({
                    "kind": "artifact",
                    "payload": {
                        "request_id": binding.request_id,
                        "path": str((self.config.artifact_root / binding.request_id / relative_path).resolve()),
                        "consent": consent,
                    },
                })
                if not submitted:
                    result["warnings"] = list(result.get("warnings", [])) + [
                        "Artifact upload could not be queued"
                    ]
        self.pending = None
        self.current_turn = None
        self.last_result = _public_result(result)
        state = "success" if result.get("status") == "success" else str(result.get("status", "failed"))
        self._send_state(state, result=self.last_result)

    def replay_pending_reports(self) -> int:
        """Queue exact completed mutation reports after an Add-in restart."""

        self._assert_main_thread()
        if not hasattr(self.dispatcher, "journal"):
            return 0
        submitted = 0
        for report in self.dispatcher.journal.pending_agent_reports():
            if not self.transport.submit({"kind": "report", "payload": report}):
                break
            submitted += 1
        return submitted

    def _capture_context(self) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        payload = {
            "request_id": request_id,
            "timeout_ms": min(30000, int(self.config.request_timeout_s * 1000)),
            "query": {
                "sections": list(CONTEXT_SECTIONS),
                "max_depth": 8,
                "include_suppressed": True,
                "connector_instance_id": self.config.connector_instance_id,
            },
        }
        task = {
            "protocol_version": 1, "request_id": request_id, "operation": "context",
            "intent_hash": hashlib.sha256(canonical_json(payload)).hexdigest(),
            "lease_id": str(uuid.uuid4()), "attempt": 1,
            "leased_until": self.clock() + self.config.request_timeout_s,
            "deadline": self.clock() + self.config.request_timeout_s,
            "payload": payload, "execution_context": {},
        }
        result = self.dispatcher.dispatch(task, CancelToken())
        data = result.get("data") if isinstance(result, dict) else None
        if result.get("status") != "success" or not isinstance(data, dict):
            raise ControllerError("CONTEXT_READ_FAILED", "Fusion context could not be read")
        if data.get("truncated") is True:
            raise ControllerError("CONTEXT_TRUNCATED", "Fusion context is truncated; Agent planning is disabled")
        if not isinstance(data.get("document"), dict) or not data["document"].get("document_id"):
            raise ControllerError("CONTEXT_READ_FAILED", "Fusion context has no active document")
        return data

    def _task(self, action: dict[str, Any], request_id: str, intent_hash: str) -> dict[str, Any]:
        artifact_dir = (self.config.artifact_root / request_id).resolve()
        return {
            "protocol_version": 1, "request_id": request_id, "operation": "execute",
            "intent_hash": intent_hash, "lease_id": str(uuid.uuid4()), "attempt": 1,
            "leased_until": self.clock() + self.config.request_timeout_s,
            "deadline": self.clock() + self.config.request_timeout_s,
            "payload": action,
            "execution_context": {
                "artifact_dir": str(artifact_dir),
                "artifact_root_fingerprint": self.config.artifact_root_fingerprint,
            },
        }

    def _assert_main_thread(self) -> None:
        if threading.get_ident() != self.main_thread_id:
            raise ControllerError("THREAD_VIOLATION", "Fusion and Palette operations require the captured main thread")

    def _mark_obsolete(self, request_id: str) -> None:
        self.obsolete_request_ids.append(request_id)
        if len(self.obsolete_request_ids) > 32:
            del self.obsolete_request_ids[:-32]

    def _send_state(self, state: str, **payload: Any) -> None:
        if self.stopped:
            return
        message = {"state": state, **payload, "error": None}
        self.send_to_palette(
            "controller_state",
            json.dumps(message, allow_nan=False, ensure_ascii=False, separators=(",", ":")),
        )

    def _send_error(self, error: ControllerError) -> None:
        if self.stopped:
            return
        self.send_to_palette(
            "controller_state",
            json.dumps(
                {"state": "failed", "error": error.as_dict()},
                allow_nan=False, ensure_ascii=False, separators=(",", ":"),
            ),
        )


# A short alias is convenient in lifecycle wiring and external contract tests.
LocalApprovalStore = ApprovalLedger
