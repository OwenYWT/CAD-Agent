"""Strict wire contract for the direct Fusion Desktop -> Cloud Agent flow.

This module deliberately imports the shared ``CadAction`` and ``ContextData``
models instead of defining a second CAD dialect.  The cloud agent can propose
one typed action; it can never return source code or an executable callable.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contract import (
    ACTION_NAMES,
    ActionData,
    CadAction,
    CadCapabilities,
    CadResult,
    ContextData,
    StrictModel,
)
from .policy import sha256_canonical

AGENT_CONTRACT_VERSION = "1.0.0"
CONTEXT_FINGERPRINT_VERSION = "ctx-c14n-1"
MAX_AGENT_PROMPT_CHARS = 8_000
MAX_AGENT_CONTEXT_BYTES = 1 * 1024 * 1024
MAX_AGENT_ARTIFACT_BYTES = 64 * 1024 * 1024

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_CONTEXT_FINGERPRINT = re.compile(r"^ctx-c14n-1:[0-9a-f]{64}$")
_SAFE_BASENAME = re.compile(r"^[^/\\\x00-\x1f]{1,255}$")


class AgentProtocolCapabilities(StrictModel):
    """Capabilities offered by this Cloud Agent contract endpoint."""

    contract_versions: list[str] = Field(
        default_factory=lambda: [AGENT_CONTRACT_VERSION], min_length=1, max_length=8
    )
    actions: list[str] = Field(default_factory=lambda: list(ACTION_NAMES), max_length=len(ACTION_NAMES))
    max_actions_per_plan: Literal[1] = 1
    max_turn_bytes: int = MAX_AGENT_CONTEXT_BYTES
    max_artifact_bytes: int = MAX_AGENT_ARTIFACT_BYTES
    artifact_upload: bool = True
    f3d_requires_separate_authorization: Literal[True] = True

    @field_validator("contract_versions")
    @classmethod
    def versions_are_supported(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("contract_versions must be unique")
        return values

    @field_validator("actions")
    @classmethod
    def actions_are_typed(cls, values: list[str]) -> list[str]:
        unsupported = sorted(set(values).difference(ACTION_NAMES))
        if unsupported:
            raise ValueError(f"unsupported typed actions: {unsupported}")
        if len(values) != len(set(values)):
            raise ValueError("actions must be unique")
        return values


# A concise compatibility name for consumers that do not distinguish Fusion
# capabilities from protocol capabilities.
AgentCapabilities = AgentProtocolCapabilities


class AgentHeartbeatRequest(StrictModel):
    contract_version: Literal[AGENT_CONTRACT_VERSION] = AGENT_CONTRACT_VERSION
    connector_instance_id: uuid.UUID
    fusion_version: str = Field(min_length=1, max_length=128)
    addin_version: str = Field(min_length=1, max_length=128)
    platform: Literal["windows", "macos"]
    capabilities: CadCapabilities
    sent_at: datetime

    @field_validator("sent_at")
    @classmethod
    def heartbeat_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("sent_at must include a timezone")
        return value


class AgentHeartbeatReceipt(StrictModel):
    connector_instance_id: uuid.UUID
    status: Literal["online"] = "online"
    heartbeat_interval_s: int = 5
    heartbeat_ttl_s: int = 15
    received_at: datetime


class AgentConnectorStatus(StrictModel):
    connector_instance_id: uuid.UUID
    connector_online: bool
    fusion_running: bool | None
    last_heartbeat_at: datetime | None = None
    fusion_version: str | None = None
    addin_version: str | None = None
    platform: Literal["windows", "macos"] | None = None
    capabilities: CadCapabilities | None = None


def negotiate_contract_version(client_versions: list[str]) -> str:
    """Return the newest mutually supported exact version, or fail closed."""

    supported = AgentProtocolCapabilities().contract_versions
    for version in reversed(supported):
        if version in client_versions:
            return version
    raise ValueError("no compatible Fusion Cloud Agent contract version")


def _entity_projection(items) -> list[dict[str, object]]:
    projected = [
        {
            "id": item.id,
            "kind": item.kind,
            "component_id": item.component_id,
            "health_state": item.health_state,
        }
        for item in items
    ]
    return sorted(projected, key=lambda item: (str(item["id"]), str(item["component_id"] or "")))


def context_fingerprint_payload(context: ContextData) -> dict[str, object]:
    """Build the privacy-bounded, order-independent context state projection.

    Names, user identity, prompt text, material descriptions, mass properties and
    vendor extensions are deliberately excluded.  Fields that can make a proposal
    stale are included.  Timeline text is included because warnings/errors can
    change without changing a persistent entity token.
    """

    document = context.document
    cloud = context.cloud
    return {
        "version": "1",
        "document": None
        if document is None
        else {
            "document_id": document.document_id,
            "is_saved": document.is_saved,
            "is_modified": document.is_modified,
            "is_read_only": document.is_read_only,
        },
        "cloud": None
        if cloud is None
        else {
            "data_file_id": cloud.data_file_id,
            "version_id": cloud.version_id,
            "version_number": cloud.version_number,
            "project_id": cloud.project_id,
            "folder_id": cloud.folder_id,
            "is_complete": cloud.is_complete,
            "is_read_only": cloud.is_read_only,
        },
        "selection": _entity_projection(context.selection),
        "features": _entity_projection(context.features),
        "parameters": sorted(
            [
                {
                    "id": parameter.id,
                    "component_id": parameter.component_id,
                    "created_by_id": parameter.created_by_id,
                    "expression": parameter.expression,
                }
                for parameter in context.parameters
            ],
            key=lambda item: (str(item["id"]), str(item["component_id"] or "")),
        ),
        "timeline": sorted(
            [
                {
                    "index": item.index,
                    "name": item.name,
                    "kind": item.kind,
                    "entity_id": item.entity_id,
                    "health_state": item.health_state,
                    "is_group": item.is_group,
                    "is_rolled_back": item.is_rolled_back,
                    "is_suppressed": item.is_suppressed,
                    "parent": None
                    if item.parent is None
                    else {"index": item.parent.index, "name": item.parent.name},
                    "error_or_warning": item.error_or_warning,
                }
                for item in context.timeline
            ],
            key=lambda item: (int(item["index"]), str(item["name"]), str(item["entity_id"] or "")),
        ),
    }


def context_fingerprint(context: ContextData) -> str:
    return f"{CONTEXT_FINGERPRINT_VERSION}:{sha256_canonical(context_fingerprint_payload(context))}"


class AgentTurnRequest(StrictModel):
    contract_version: Literal[AGENT_CONTRACT_VERSION] = AGENT_CONTRACT_VERSION
    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    connector_instance_id: uuid.UUID
    prompt: str = Field(min_length=1, max_length=MAX_AGENT_PROMPT_CHARS)
    context: ContextData
    context_fingerprint: str = Field(pattern=_CONTEXT_FINGERPRINT.pattern)
    capabilities: CadCapabilities
    export_artifact_upload_consent: bool = False
    f3d_upload_authorized: bool = False

    @field_validator("prompt")
    @classmethod
    def prompt_is_not_whitespace(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("prompt must not be blank")
        return stripped

    @model_validator(mode="after")
    def context_and_consent_are_bound(self) -> "AgentTurnRequest":
        serialized = json.dumps(
            self.context.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(serialized) > MAX_AGENT_CONTEXT_BYTES:
            raise ValueError("context exceeds the direct Agent size limit")
        if context_fingerprint(self.context) != self.context_fingerprint:
            raise ValueError("context fingerprint does not match the supplied context")
        if self.f3d_upload_authorized and not self.export_artifact_upload_consent:
            raise ValueError("F3D upload authorization also requires explicit export artifact upload consent")
        return self


PlanStatus = Literal["proposed", "needs_clarification", "no_action"]


class AgentPlanResponse(StrictModel):
    contract_version: Literal[AGENT_CONTRACT_VERSION] = AGENT_CONTRACT_VERSION
    request_id: uuid.UUID
    proposal_id: uuid.UUID
    connector_instance_id: uuid.UUID
    status: PlanStatus
    context_fingerprint: str = Field(pattern=_CONTEXT_FINGERPRINT.pattern)
    action: CadAction | None = None
    risk: Literal["low", "medium", "high"] | None = None
    question: str | None = Field(default=None, min_length=1, max_length=1_000)
    reason: str | None = Field(default=None, min_length=1, max_length=2_000)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def status_fields_are_exclusive(self) -> "AgentPlanResponse":
        if self.status == "proposed":
            if self.action is None or self.risk is None or self.expires_at is None:
                raise ValueError("proposed plans require exactly one action, risk and expiry")
            if self.question is not None:
                raise ValueError("proposed plans cannot include a clarification question")
            if self.expires_at.tzinfo is None:
                raise ValueError("expires_at must include a timezone")
        else:
            if self.action is not None or self.risk is not None or self.expires_at is not None:
                raise ValueError("non-proposal plans cannot include an action, risk or expiry")
            if self.status == "needs_clarification" and self.question is None:
                raise ValueError("needs_clarification requires a question")
            if self.status == "no_action" and self.reason is None:
                raise ValueError("no_action requires a reason")
        return self


class AgentExecutionReport(StrictModel):
    contract_version: Literal[AGENT_CONTRACT_VERSION] = AGENT_CONTRACT_VERSION
    report_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    request_id: uuid.UUID
    proposal_id: uuid.UUID
    connector_instance_id: uuid.UUID
    context_fingerprint: str = Field(pattern=_CONTEXT_FINGERPRINT.pattern)
    action_intent_hash: str = Field(pattern=_HEX_64.pattern)
    result: CadResult[ActionData]
    completed_at: datetime

    @model_validator(mode="after")
    def result_is_bound_to_request(self) -> "AgentExecutionReport":
        if self.result.request_id != self.request_id:
            raise ValueError("result request_id does not match the execution report")
        if self.completed_at.tzinfo is None:
            raise ValueError("completed_at must include a timezone")
        if self.result.status not in {"success", "failed", "cancelled", "timeout", "indeterminate"}:
            raise ValueError("execution reports accept only terminal local execution states")
        if self.result.status != "success":
            if self.result.error is None:
                raise ValueError("non-success execution reports require a structured error")
            return self
        if self.result.error is not None:
            raise ValueError("successful execution reports cannot include an error")
        expected_kind = (
            "save"
            if self.result.action in {"cad.save_document", "cad.save_as"}
            else "export"
            if self.result.action == "cad.export"
            else "mutation"
        )
        if self.result.data is None or self.result.data.kind != expected_kind:
            raise ValueError("successful execution result kind does not match its action")
        verification = self.result.verification
        if (
            verification is None
            or not verification.passed
            or not verification.compute_completed
            or verification.new_feature_errors
            or not verification.checks
            or not all(check.passed for check in verification.checks)
        ):
            raise ValueError("successful execution reports require observed rebuild verification")
        if expected_kind == "mutation" and not self.result.changes:
            raise ValueError("successful mutations require an observed structured diff")
        if expected_kind == "save" and not self.result.data.local_save_accepted:
            raise ValueError("successful saves require local save acceptance evidence")
        if expected_kind == "export" and not self.result.data.artifact_ids:
            raise ValueError("successful exports require verified artifact identifiers")
        return self


class AgentReportReceipt(StrictModel):
    request_id: uuid.UUID
    report_id: uuid.UUID
    status: Literal["accepted", "duplicate"]
    received_at: datetime


class AgentArtifactUploadClaim(StrictModel):
    request_id: uuid.UUID
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, le=MAX_AGENT_ARTIFACT_BYTES)
    sha256: str = Field(pattern=_HEX_64.pattern)

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if value in {".", ".."} or not _SAFE_BASENAME.fullmatch(value):
            raise ValueError("artifact filename must be a safe basename")
        return value


class AgentArtifactReceipt(StrictModel):
    request_id: uuid.UUID
    filename: str
    size_bytes: int
    sha256: str
    status: Literal["accepted", "duplicate"]
    download_url: str | None = None
