"""Bounded, read-only L2 queries over integrity-verified native kernel state."""
import json
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.execution.canonical import canonical_sha256
from app.freecad.reference_geometry import normalize_reference_state

if TYPE_CHECKING:
    from app.freecad.contracts import FreeCADOperationPlan


class InspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    objects: list[str] = Field(min_length=1, max_length=8)
    fields: list[Literal["properties", "constraints", "geometry", "topology", "dependencies"]] = Field(min_length=1, max_length=5)
    offset: int = Field(default=0, ge=0, le=5000, strict=True)
    limit: int = Field(default=16, ge=1, le=32, strict=True)


def _constraint_expression_bindings(obj: dict, constraint: dict) -> list[dict]:
    paths = {f"Constraints[{constraint['index']}]"}
    if constraint.get('name'):
        paths.add('Constraints.' + constraint['name'])
    return [dict(binding) for binding in obj.get('expressions', {}).get('items', [])
            if binding.get('property') in paths and binding.get('expression')]


def validate_constraint_edit_authority(plan: 'FreeCADOperationPlan', state: dict) -> None:
    """Reject known expression-owned edits while the Agent can still correct its plan.

    Unrecorded/new constraints remain subject to native execution checks. This
    check neither removes expressions nor substitutes values or source edits.
    """
    objects = {obj['name']: obj for obj in state.get('objects', [])}
    for operation in plan.operations:
        if operation.action != 'sketch.set_constraint':
            continue
        args = operation.args
        obj = objects.get(args['sketch'], {})
        constraints = obj.get('inspection', {}).get('constraints', {}).get('items', [])
        constraint = next((row for row in constraints
                           if row['index'] == args['constraint_index']), None)
        if constraint is None:
            continue
        bindings = _constraint_expression_bindings(obj, constraint)
        if bindings:
            raise ValueError(
                f"sketch_constraint_expression_protected: {args['sketch']} constraint "
                f"{args['constraint_index']} is driven by {json.dumps(bindings, ensure_ascii=False)}. "
                "Preserve this relationship and edit its source dimension only. "
                "Inspect the source sketch if its constraint identity is not known."
            )


def inspect_state(state: dict, request: InspectionRequest) -> dict:
    state = normalize_reference_state(state)
    by_name = {o["name"]: o for o in state.get("objects", [])}
    result = []
    budget = 16000
    for name in dict.fromkeys(request.objects):
        obj = by_name.get(name)
        if obj is None:
            result.append({"name": name, "error": "object_not_found"})
            continue
        detail = {"name": name, "type_id": obj.get("type_id"), "is_valid": obj.get("is_valid")}
        if obj.get("reference_geometry"):
            detail["reference_geometry"] = obj["reference_geometry"]
        for field in dict.fromkeys(request.fields):
            if field == "properties":
                values = [{"name": k, "value": str(v)[:500] if isinstance(v,str) else v}
                          for k,v in obj.get("properties", {}).items()]
                known = len(values)
            elif field == "dependencies":
                values = obj.get("out", [])
                known = len(values)
            else:
                stored = obj.get("inspection", {}).get(field)
                if stored is None:
                    detail[field] = {"status": "unavailable", "reason": "not_recorded_in_kernel_checkpoint"}
                    continue
                if stored.get("status") == "not_applicable":
                    detail[field] = dict(stored)
                    continue
                values = stored["items"]
                known = stored["total"]
                if field == 'constraints':
                    values = [
                        {**row, 'expression_bindings': bindings,
                         'edit_policy': 'source_dimension_only'} if bindings else row
                        for row in values
                        for bindings in [_constraint_expression_bindings(obj, row)]
                    ]
            page = []
            for item in values[request.offset:request.offset+request.limit]:
                size = len(json.dumps(item,ensure_ascii=False).encode("utf-8"))
                if size > budget:
                    break
                page.append(item)
                budget -= size
            detail[field] = {"status": "measured", "items": page, "total": known,
                             "offset": request.offset, "returned": len(page),
                             "omitted": max(0,known-len(page)), "recorded": len(values)}
        result.append(detail)
    return {"schema_version": "cad-inspection.v1", "state_hash": canonical_sha256(state),
            "source": "verified_kernel_checkpoint", "objects": result}
