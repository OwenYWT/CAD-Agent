"""Agent tool wire contract for the durable FreeCAD workflow.

Reads run against the release catalog or a verified workflow checkpoint. The
execute call is deliberately *deferred*: generation persists its arguments and
the existing fenced CAD activity executes them. Planning never emits a successful
execution result, and model arguments cannot select tenants, storage or approvals.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.freecad.api_catalog import CapabilityQuery, query_capabilities
from app.freecad.contracts import (
    DocumentExportArgs, FreeCADAPIArgs, FreeCADOperation, FreeCADOperationPlan,
    FrozenContract, ObjectName, OperationId, capability_schemas,
)
from app.freecad.inspection import InspectionRequest, inspect_state
from app.contracts.constraint_patch import ConstraintPatch


OperationAction = FreeCADOperation.model_fields["action"].annotation


class OperationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: OperationAction


EXECUTE_TOOL = "freecad_execute"
API_EXECUTE_TOOL = "freecad_execute_api"
CONSTRAINT_PATCH_TOOL = 'freecad_patch_constraints'
FAILURE_INSPECTION_TOOL = 'freecad_inspect_failure'


class NativeProgram(FrozenContract):
    op_id: OperationId
    args: FreeCADAPIArgs


class NativeExportArgs(DocumentExportArgs):
    objects: tuple[ObjectName, ...] = Field(min_length=1)


class NativeExport(FrozenContract):
    op_id: OperationId
    args: NativeExportArgs


class NativeExecutionRequest(FrozenContract):
    """One native program and its explicit export, never a mixed operation list."""
    document_name: ObjectName = "Model"
    execution_mode: Literal["final", "checkpoint"] = "final"
    execute: NativeProgram
    export: NativeExport


def execution_plan(name: str, arguments: dict) -> FreeCADOperationPlan:
    """Compile tool envelopes into the unchanged, fenced runtime plan contract."""
    if name == EXECUTE_TOOL:
        operations = arguments.get('operations')
        if isinstance(operations, (list, tuple)) and any(
            isinstance(op, dict) and op.get('action') == 'api.execute' for op in operations
        ):
            raise ValueError(
                'freecad_execute accepts typed operations only. sketch.create creates its Body '
                'when absent. For a native program use freecad_execute_api with execute and '
                'export fields; do not mix Python and typed operations in one transaction.'
            )
        return FreeCADOperationPlan.model_validate(arguments)
    if name == API_EXECUTE_TOOL:
        request = NativeExecutionRequest.model_validate(arguments)
        return FreeCADOperationPlan.model_validate({
            'document_name': request.document_name, 'execution_mode': request.execution_mode,
            'operations': [
                {**request.execute.model_dump(mode='json'), 'action': 'api.execute'},
                {**request.export.model_dump(mode='json'), 'action': 'document.export'},
            ],
        })
    raise ValueError(f'unknown FreeCAD execution tool: {name}')


class FailureInspection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fields: list[Literal['constraints', 'geometry', 'construction', 'planned_constraints', 'solver', 'references']] = Field(min_length=1, max_length=6)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=16, ge=1, le=64)


def read_failure(arguments, context):
    request = FailureInspection.model_validate(arguments)
    sketch = next(s for s in context['snapshot']['sketches'] if s['name'] == context['sketch'])
    fields = {'constraints': sketch['constraints'], 'geometry': sketch['geometry'],
              'construction': context['construction_geometry'], 'planned_constraints': context['planned_constraints'],
              'solver': sketch['solver'], 'references': context['snapshot']['references']}
    result = {key: context[key] for key in ('sketch', 'plan_hash', 'checkpoint_hash', 'diagnostic_hash')}
    result.update(source='verified_failure_diagnostic_and_frozen_construction', valid_checkpoint=False)
    for field in request.fields:
        value = fields[field]
        result[field] = {'items': value[request.offset:request.offset + request.limit],
                        'total': len(value), 'offset': request.offset,
                        'next_offset': request.offset + request.limit if request.offset + request.limit < len(value) else None} if isinstance(value, list) else value
    return result


def missing_failure_inspection(context, inspections):
    """Proposals must account for both observed and not-yet-executed constraints."""
    native = next(s for s in context['snapshot']['sketches'] if s['name'] == context['sketch'])
    totals = {'constraints': len(native['constraints']), 'construction': len(context['construction_geometry']),
              'planned_constraints': len(context['planned_constraints'])}
    missing = []
    results = [item['result'] for item in inspections]
    for field, total in totals.items():
        seen = set()
        for result in results:
            if field in result:
                page = result[field]
                seen.update(range(page['offset'], page['offset'] + len(page['items'])))
        gap = next((index for index in range(total) if index not in seen), None)
        if gap is not None:
            missing.append({'fields': [field], 'offset': gap, 'limit': min(64, total - gap)})
    if not any('solver' in result for result in results):
        missing.append({'fields': ['solver']})
    return missing
_READS = {
    "freecad_discover": (CapabilityQuery, "Look up installed FreeCAD APIs, commands and workbenches. Discovery is not functional verification."),
    "freecad_describe_operation": (OperationQuery, "Read the exact argument schema for a typed modeling operation before using it."),
    "freecad_inspect": (InspectionRequest, "Read properties, constraints, geometry and topology from the verified current document checkpoint; unavailable measurements remain unknown."),
}


def tool_schemas(*, has_document: bool, constraint_repair: bool = False) -> list[dict[str, Any]]:
    if constraint_repair:
        return [{'type': 'function', 'function': {'name': name, 'description': description,
                 'parameters': model.model_json_schema()}} for name, model, description in (
            (FAILURE_INSPECTION_TOOL, FailureInspection,
             'Inspect the actual failed sketch, logical constraint identities, execution progress and frozen construction inputs. Failure evidence is not a valid checkpoint.'),
            (CONSTRAINT_PATCH_TOOL, ConstraintPatch,
             'Propose only constraint additions, replacements or deletions on the diagnosed sketch. Use logical IDs, never native ordinals. Backend verifies authority, mathematical relationships and native parameter behavior; a proposal is not successful execution.'))]
    specs = [
        (name, model, description) for name, (model, description) in _READS.items()
        if has_document or name != "freecad_inspect"
    ]
    deferred = (
        "The durable workflow executes this call after persisting its arguments, then independently "
        "checks the artifacts. This does not approve, commit, or certify fit. No user confirmation fields are accepted.")
    specs.extend([
        (EXECUTE_TOOL, FreeCADOperationPlan,
         "Build a candidate using typed operations only, ending with document.export. "
         "sketch.create creates the named PartDesign::Body if absent. "
         "api.execute is not accepted here; use freecad_execute_api for native programs. " + deferred),
        (API_EXECUTE_TOOL, NativeExecutionRequest,
         "Use when typed operations cannot express the required native capability. "
         "Submit one Python program in execute.args.source and explicitly select export.args.objects. "
         "This compiles to exactly api.execute followed by document.export; typed operations cannot be mixed in. "
         "Selected-feature edits require scoped typed operations. " + deferred),
    ])
    tools = []
    for name, model, description in specs:
        schema = model.model_json_schema()
        if name == EXECUTE_TOOL:
            # The persisted/runtime v1 contract still accepts retained native plans.
            # Only this Agent tool's public action set is narrowed.
            schema['$defs']['FreeCADOperation']['properties']['action']['enum'].remove('api.execute')
        tools.append({'type': 'function', 'function': {
            'name': name, 'description': description, 'parameters': schema}})
    return tools


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
