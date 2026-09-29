"""Protect model intent while a provider proposes a sketch-constraint repair."""
from __future__ import annotations

from .contracts import FreeCADOperationPlan


SKETCH_FAILURES = frozenset({
    'sketch_redundant_constraints', 'sketch_conflicting_constraints',
    'sketch_under_constrained',
})


def validate_subtractive_repair(before: FreeCADOperationPlan, after: FreeCADOperationPlan,
                                failure: dict) -> None:
    """Preserve feature dimensions; placement repair needs independent acceptance."""
    if failure.get('error_code') != 'subtractive_feature_no_effect':
        return
    from app.contracts.acceptance import AcceptanceContract
    acceptance = failure.get('engineering_acceptance')
    contract = AcceptanceContract.model_validate(acceptance) if acceptance else None
    target = failure.get('operation_id')
    if not target or before.document_name != after.document_name or len(before.operations) != len(after.operations):
        raise ValueError('cut repair must preserve document and every operation')
    found = False
    failed = next((op for op in before.operations if op.op_id == target), None)
    profile = failed.args.get('profile') if failed is not None else None
    # A shared sketch is not a local repair target: moving it would also change
    # another feature's placement. A base-document sketch is equally unowned.
    shared = any(op.op_id != target and (op.args.get('profile')==profile
        or profile in (op.args.get('profiles') or ())) for op in before.operations)
    for old, new in zip(before.operations, after.operations):
        if old.op_id != target:
            if old != new:
                placement = (old.action == new.action == 'sketch.create' and old.op_id==new.op_id
                    and old.args.get('name')==profile and not shared and contract is not None
                    and not contract.unresolved and any(c.required for c in contract.checks))
                fields={'frame','plane','offset_mm','reversed'}
                if not placement or ({k:v for k,v in old.args.items() if k not in fields}
                    != {k:v for k,v in new.args.items() if k not in fields}):
                    raise ValueError('cut repair cannot change other operations without scoped acceptance')
            continue
        found = True
        if old.action not in {'feature.pocket', 'feature.hole'} or old.op_id != new.op_id or old.action != new.action:
            raise ValueError('cut repair must target the failed subtractive feature')
        protected_old = {k: v for k, v in old.args.items() if k != 'reversed'}
        protected_new = {k: v for k, v in new.args.items() if k != 'reversed'}
        if protected_old != protected_new:
            raise ValueError('cut repair cannot change dimensions, profile, hole type or target')
        if (failure.get('details') or {}).get('direction_locked') and old.args.get('reversed') != new.args.get('reversed'):
            raise ValueError('cut direction is explicitly locked; request clarification')
    if not found:
        raise ValueError('failed operation is absent from repair source')


def validate_constraint_repair(before: FreeCADOperationPlan, after: FreeCADOperationPlan,
                               failure: dict) -> None:
    code = failure.get('error_code')
    if code not in SKETCH_FAILURES:
        return
    sketch = (failure.get('details') or {}).get('object')
    if not isinstance(sketch, str) or not sketch:
        raise ValueError('constraint repair requires the diagnosed sketch identity')
    if before.document_name != after.document_name:
        raise ValueError('constraint repair cannot replace the document')

    def is_target_constraint(op):
        return op.action == 'sketch.add_constraint' and op.args.get('sketch') == sketch

    def is_dimension(op):
        return op.args.get('value_mm') is not None

    # All non-constraint operations and all unrelated sketches stay byte-for-byte
    # equivalent after contract canonicalization, including their execution order.
    old_fixed = [op for op in before.operations if not is_target_constraint(op)]
    new_fixed = [op for op in after.operations if not is_target_constraint(op)]
    if old_fixed != new_fixed:
        raise ValueError('constraint repair cannot change geometry, features, exports or unrelated sketches')

    old_target = [op for op in before.operations if is_target_constraint(op)]
    new_target = [op for op in after.operations if is_target_constraint(op)]
    if not old_target and not any(op.action == 'sketch.create' and op.args.get('name') == sketch
                                  for op in before.operations):
        raise ValueError('constraint repair cannot modify an unowned base sketch')
    old_dimensions = [op for op in old_target if is_dimension(op)]
    new_dimensions = [op for op in new_target if is_dimension(op)]
    if code == 'sketch_under_constrained':
        # An underconstrained sketch needs additions; removing an existing
        # relation would change intent rather than fill a missing condition.
        remaining = iter(new_target)
        if any(not any(candidate == op for candidate in remaining) for op in old_target):
            raise ValueError('constraint repair must preserve every existing constraint when adding missing ones')
    elif old_dimensions != new_dimensions:
        raise ValueError('constraint repair cannot change or remove dimensional constraints')
