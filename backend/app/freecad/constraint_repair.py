"""Protect model intent while a provider proposes a sketch-constraint repair."""
from __future__ import annotations

import json

from .contracts import FreeCADOperationPlan
from .failure_snapshot import digest
from .repair_differences import RepairRejected, operation_differences


SKETCH_FAILURES = frozenset({
    'sketch_redundant_constraints', 'sketch_conflicting_constraints',
    'sketch_under_constrained', 'sketch_solver_failed', 'sketch_parameter_dependency_failed',
})


def validate_subtractive_repair(before: FreeCADOperationPlan, after: FreeCADOperationPlan,
                                failure: dict) -> None:
    """Preserve feature dimensions; placement repair needs independent acceptance."""
    if failure.get('error_code') != 'subtractive_feature_no_effect':
        return
    from app.contracts.acceptance_v2 import decode_acceptance
    acceptance = failure.get('engineering_acceptance')
    contract = decode_acceptance(acceptance) if acceptance else None
    target = failure.get('operation_id')
    def rejected(reason):
        return RepairRejected('operation_repair_rejected', reason,
                              operation_differences(before.operations, after.operations))
    if not target or before.document_name != after.document_name or len(before.operations) != len(after.operations):
        raise rejected('cut repair must preserve document and every operation')
    found = False
    failed = next((op for op in before.operations if op.op_id == target), None)
    profile = failed.args.get('profile') if failed is not None else None
    # A shared sketch is not a local repair target: moving it would also change
    # another feature's placement. A base-document sketch is equally unowned.
    shared = any(op.op_id != target and (op.args.get('profile')==profile or op.args.get('path')==profile
        or profile in (op.args.get('profiles') or ())) for op in before.operations)
    for old, new in zip(before.operations, after.operations):
        if old.op_id != target:
            if old != new:
                placement = (old.action == new.action == 'sketch.create' and old.op_id==new.op_id
                    and old.args.get('name')==profile and not shared and contract is not None
                    and not contract.unresolved and not contract.verification_limits
                    and any(c.required for c in contract.checks))
                fields={'frame','plane','offset_mm','reversed'}
                if not placement or ({k:v for k,v in old.args.items() if k not in fields}
                    != {k:v for k,v in new.args.items() if k not in fields}):
                    raise rejected('cut repair cannot change other operations without scoped acceptance')
            continue
        found = True
        if old.action not in {'feature.pocket', 'feature.hole'} or old.op_id != new.op_id or old.action != new.action:
            raise rejected('cut repair must target the failed subtractive feature')
        protected_old = {k: v for k, v in old.args.items() if k != 'reversed'}
        protected_new = {k: v for k, v in new.args.items() if k != 'reversed'}
        if protected_old != protected_new:
            raise rejected('cut repair cannot change dimensions, profile, hole type or target')
        if (failure.get('details') or {}).get('direction_locked') and old.args.get('reversed') != new.args.get('reversed'):
            raise rejected('cut direction is explicitly locked; request clarification')
    if not found:
        raise rejected('failed operation is absent from repair source')


def compile_cut_patch(before, proposal, failure):
    from app.contracts.cut_patch import CutPatch
    patch = CutPatch.model_validate(proposal)
    expected = {'plan_hash': digest(before.model_dump(mode='json')), 'operation_id': failure.get('operation_id')}
    for field, value in expected.items():
        if getattr(patch, field) != value:
            raise RepairRejected('operation_repair_rejected', 'cut patch baseline/target does not match failure',
                [{'field': field, 'before': value, 'after': getattr(patch, field)}])
    failed = next((op for op in before.operations if op.op_id == patch.operation_id), None)
    if failed is None or failed.action not in {'feature.pocket', 'feature.hole'}:
        raise RepairRejected('operation_repair_rejected', 'failed cut is absent from source')
    evidence = json.loads((failure.get('details') or {}).get('cut_diagnostic_json') or '{}')
    if (evidence.get('schema_version') == 'subtractive-evidence.v2' and patch.placement is None
            and evidence.get('classification') in {'profile_outside_material', 'insufficient_reach'}):
        raise RepairRejected('operation_repair_rejected',
            'native intersections show direction alone cannot reach material; use an authorized private sketch placement or request clarification',
            [{'operation_id': patch.operation_id, 'field': 'cut_intersection', 'before': evidence,
              'after': {'reversed': patch.reversed}}])
    operations = []
    placed = False
    for op in before.operations:
        raw = op.model_dump(mode='json')
        if op.op_id == patch.operation_id and patch.reversed is not None:
            raw['args']['reversed'] = patch.reversed
        if patch.placement is not None and op.action == 'sketch.create' and op.args['name'] == failed.args['profile']:
            raw['args'] = {**raw['args'], **patch.placement.model_dump(mode='json')}
            placed = True
        operations.append(raw)
    if patch.placement is not None and not placed:
        raise RepairRejected('operation_repair_rejected', 'base-document sketch placement is not owned by this repair')
    after = FreeCADOperationPlan.model_validate({**before.model_dump(mode='json'), 'operations': operations})
    validate_subtractive_repair(before, after, failure)
    if before == after:
        raise RepairRejected('operation_repair_rejected', 'cut patch made no change')
    return after


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
