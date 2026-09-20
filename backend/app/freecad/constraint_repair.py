"""Protect model intent while a provider proposes a sketch-constraint repair."""
from __future__ import annotations

from .contracts import FreeCADOperationPlan


SKETCH_FAILURES = frozenset({
    'sketch_redundant_constraints', 'sketch_conflicting_constraints',
    'sketch_under_constrained',
})


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
