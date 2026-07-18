#!/usr/bin/env python3
"""Generate deterministic Fusion connector JSON Schemas from Pydantic models."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.fusion360.contract import (  # noqa: E402
    CAD_ACTION_ADAPTER,
    CONTRACT_VERSION,
    ActionData,
    CadCapabilities,
    CadResult,
    ContextData,
    ContextRequest,
    RegisterRequest,
    RegisterResponse,
    TaskControl,
    TaskLease,
    TaskResultEnvelope,
    VerificationData,
    VerifyRequest,
)
from app.fusion360.agent_contract import (  # noqa: E402
    AgentConnectorStatus,
    AgentExecutionReport,
    AgentHeartbeatRequest,
    AgentPlanResponse,
    AgentTurnRequest,
)

SCHEMA_DIR = ROOT / "schemas" / "fusion360"


def _schemas() -> dict[str, dict[str, Any]]:
    return {
        "agent-turn": AgentTurnRequest.model_json_schema(),
        "agent-plan": AgentPlanResponse.model_json_schema(),
        "agent-execution-report": AgentExecutionReport.model_json_schema(),
        "agent-heartbeat": AgentHeartbeatRequest.model_json_schema(),
        "agent-connector-status": AgentConnectorStatus.model_json_schema(),
        "cad-action": CAD_ACTION_ADAPTER.json_schema(),
        "capabilities": CadCapabilities.model_json_schema(),
        "context-request": ContextRequest.model_json_schema(),
        "context-result": CadResult[ContextData].model_json_schema(),
        "action-result": CadResult[ActionData].model_json_schema(),
        "verify-request": VerifyRequest.model_json_schema(),
        "verify-result": CadResult[VerificationData].model_json_schema(),
        "ipc-register-request": RegisterRequest.model_json_schema(),
        "ipc-register-response": RegisterResponse.model_json_schema(),
        "ipc-task-lease": TaskLease.model_json_schema(),
        "ipc-task-result": TaskResultEnvelope.model_json_schema(),
        "ipc-task-control": TaskControl.model_json_schema(),
    }


def _normalize(value: Any) -> Any:
    """Remove known Pydantic 2.7/2.12 presentation-only schema drift.

    The resulting schemas retain identical validation semantics while remaining
    reproducible in the repository's pinned environment and newer developer
    environments. Any structural/semantic change still changes the output.
    """
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if not isinstance(value, dict):
        return value
    normalized = {key: _normalize(item) for key, item in value.items()}
    if normalized.get("enum") == [normalized.get("const")]:
        normalized.pop("enum", None)
    all_of = normalized.get("allOf")
    if isinstance(all_of, list) and len(all_of) == 1 and set(all_of[0]) == {"$ref"}:
        normalized["$ref"] = all_of[0]["$ref"]
        normalized.pop("allOf", None)
    return normalized


def _render(name: str, schema: dict[str, Any]) -> str:
    document = {
        "$id": f"https://cad-agent.local/schemas/fusion360/{name}/{CONTRACT_VERSION}",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "x-contract-version": CONTRACT_VERSION,
        **_normalize(schema),
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if tracked schemas differ")
    args = parser.parse_args()
    drift: list[str] = []
    if not args.check:
        SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for name, schema in _schemas().items():
        path = SCHEMA_DIR / f"{name}.schema.json"
        rendered = _render(name, schema)
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                drift.append(str(path.relative_to(ROOT)))
        else:
            path.write_text(rendered, encoding="utf-8")
    if drift:
        print("Fusion schema drift: " + ", ".join(drift), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
