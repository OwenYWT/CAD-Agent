"""Agent tool wire contract for the durable FreeCAD workflow.

Reads run against the release catalog or a verified workflow checkpoint. The
execute call is deliberately *deferred*: generation persists its arguments and
the existing fenced CAD activity executes them. Planning never emits a successful
execution result, and model arguments cannot select tenants, storage or approvals.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.freecad.api_catalog import CapabilityQuery, query_capabilities
from app.freecad.contracts import FreeCADOperation, FreeCADOperationPlan, capability_schemas
from app.freecad.inspection import InspectionRequest, inspect_state


class OperationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: FreeCADOperation.model_fields["action"].annotation


EXECUTE_TOOL = "freecad_execute"
_READS = {
    "freecad_discover": (CapabilityQuery, "Look up installed FreeCAD APIs, commands and workbenches. Discovery is not functional verification."),
    "freecad_describe_operation": (OperationQuery, "Read the exact argument schema for a typed modeling operation before using it."),
    "freecad_inspect": (InspectionRequest, "Read properties, constraints, geometry and topology from the verified current document checkpoint; unavailable measurements remain unknown."),
}


def tool_schemas(*, has_document: bool) -> list[dict[str, Any]]:
    specs = [
        (name, model, description) for name, (model, description) in _READS.items()
        if has_document or name != "freecad_inspect"
    ]
    specs.append((EXECUTE_TOOL, FreeCADOperationPlan,
        "Build a candidate with FreeCAD using typed operations or api.execute for native capabilities. "
        "The durable workflow executes this call after persisting its arguments, then independently "
        "checks the artifacts. This does not approve, commit, or certify fit. No user confirmation fields are accepted."))
    return [{"type": "function", "function": {"name": name, "description": description,
            "parameters": model.model_json_schema()}} for name, model, description in specs]


def read_tool(name: str, arguments: dict, *, state: dict | None) -> dict:
    if name not in _READS:
        raise ValueError(f"unknown FreeCAD read tool: {name}")
    args = _READS[name][0].model_validate(arguments)
    if name == "freecad_discover":
        return query_capabilities(args)
    if name == "freecad_describe_operation":
        return {"action": args.action, "parameters": capability_schemas()[args.action]}
    if state is None:
        raise ValueError("document inspection requires a verified checkpoint")
    return inspect_state(state, args)

