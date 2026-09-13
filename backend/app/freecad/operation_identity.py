"""Deterministic mutation identity within a persisted FreeCAD document ledger."""
import hashlib
import json

from app.freecad.contracts import FreeCADOperationPlan


def scope_plan_to_base(plan: FreeCADOperationPlan, base_state: dict) -> FreeCADOperationPlan:
    """A retry retains IDs; a later edit (including A→B→A) gets new IDs.

    FCStd stores all previous operation IDs. Property names or requested values
    alone are not identities for a new mutation against a different checkpoint.
    The verified state includes the prior ledger, so a restored value cannot be
    mistaken for a replay. Already persisted operation plans are never rewritten.
    """
    from app.freecad.selection import validate_selected_operations
    validate_selected_operations(plan, base_state)
    base = hashlib.sha256(json.dumps(base_state, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()
    operations = []
    for operation in plan.operations:
        payload = json.dumps(operation.model_dump(mode="json"), sort_keys=True,
                             ensure_ascii=False, separators=(",", ":"))
        identity = hashlib.sha256((base + "\0" + payload).encode()).hexdigest()[:24]
        operations.append(operation.model_copy(update={"op_id": f"{operation.op_id[:60]}-{identity}"}))
    return plan.model_copy(update={"operations": tuple(operations)})
