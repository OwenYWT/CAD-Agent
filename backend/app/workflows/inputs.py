"""Stable workflow payload decoding."""
from __future__ import annotations
from typing import Any
from uuid import UUID
from app.workflows.temporal import McadAgentWorkflowV2Request

def _uuid(payload: dict[str, Any], key: str) -> UUID:
    return UUID(str(payload[key]))


def _agent_v2_request(payload: dict[str, Any]) -> McadAgentWorkflowV2Request:
    fields = McadAgentWorkflowV2Request.model_fields
    return McadAgentWorkflowV2Request.model_validate(
        {key: payload[key] for key in fields if key in payload}
    )
