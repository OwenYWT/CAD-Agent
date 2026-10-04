"""Backend composition of logical-constraint patches and verification obligations."""
from __future__ import annotations

from copy import deepcopy

from app.contracts.acceptance import AcceptanceContract
from app.contracts.constraint_patch import ConstraintPatch
from app.freecad.contracts import FreeCADOperation, FreeCADOperationPlan, FreeCADPlanningError
from app.freecad.constraint_relationships import (
    TOLERANCE, derive_row, native_args, relation_rows, satisfies, serialize_row, deserialize_row,
)
from app.freecad.failure_snapshot import digest, validate_snapshot


class PatchRejected(FreeCADPlanningError):
    def __init__(self, reason, *, sketch=None, logical_id=None, field=None, before=None, after=None):
        super().__init__('constraint_patch_rejected', reason)
        self.differences = [{'object': sketch, 'logical_id': logical_id, 'field': field,
                             'before': before, 'after': after, 'reason': reason}]


def _logical_id(operation):
    return operation.args.get('logical_id') or operation.op_id


def construction_geometry(plan, sketch, base_state):
    """Reconstruct declared construction inputs, never the solver-distorted failure."""
    created = any(op.action == 'sketch.create' and op.args['name'] == sketch for op in plan.operations)
    geometry = []
    if not created:
        base = next((o for o in (base_state or {}).get('objects', []) if o['name'] == sketch), None)
        recorded = (base or {}).get('inspection', {}).get('geometry', {})
        if recorded.get('total') != len(recorded.get('items', [])):
            raise PatchRejected('complete, verified construction geometry is required', sketch=sketch)
        geometry = deepcopy(recorded['items'])
    for op in plan.operations:
        if op.action not in {'sketch.add_geometry', 'sketch.add_profile'} or op.args['sketch'] != sketch:
            continue
        g = op.args['geometry']
        common = {'construction': bool(g.get('construction', False)), 'source_operation_id': op.op_id}
        if g['kind'] == 'line':
            geometry.append({**common, 'index': len(geometry), 'type': 'Part::GeomLineSegment',
                'start': [g['start']['x'], g['start']['y'], 0], 'end': [g['end']['x'], g['end']['y'], 0]})
        elif g['kind'] == 'circle':
            geometry.append({**common, 'index': len(geometry), 'type': 'Part::GeomCircle',
                'center': [g['center']['x'], g['center']['y'], 0], 'radius_mm': g['radius_mm']})
        elif g['kind'] == 'rectangle':
            x, y = g['corner']['x'], g['corner']['y']
            w, h = g['width_mm'], g['height_mm']
            points = [[x, y, 0], [x+w, y, 0], [x+w, y+h, 0], [x, y+h, 0]]
            for i in range(4):
                geometry.append({**common, 'index': len(geometry), 'type': 'Part::GeomLineSegment',
                    'start': points[i], 'end': points[(i+1) % 4]})
        else:
            raise PatchRejected('this curve needs a native relationship proof; automatic constraint changes are protected', sketch=sketch)
    if not geometry:
        raise PatchRejected('construction basis is unavailable', sketch=sketch)
    return geometry


def build_patch_context(before, snapshot, failure, base_state=None, prior_contract=None):
    validate_snapshot(snapshot, before.model_dump(mode='json'), snapshot.get('checkpoint_hash'), failure['operation_id'])
    sketch = (failure.get('details') or {}).get('object')
    native = next((s for s in snapshot['sketches'] if s['name'] == sketch), None)
    if native is None:
        raise PatchRejected('diagnosed sketch is absent from the verified failure', sketch=sketch)
    geometry = construction_geometry(before, sketch, base_state)
    acceptance = failure.get('engineering_acceptance')
    if not acceptance:
        raise PatchRejected('saved engineering requirements are required for automatic repair', sketch=sketch)
    acceptance = AcceptanceContract.model_validate(acceptance).model_dump(mode='json')
    if acceptance.get('unresolved') or acceptance.get('verification_limits'):
        raise PatchRejected('unresolved requirements or unsupported necessary checks require clarification', sketch=sketch)
    prior = deepcopy(prior_contract or {})
    if prior and prior['acceptance_hash'] != digest(acceptance):
        raise PatchRejected('repair cannot replace the frozen engineering requirements', sketch=sketch)
    return {'schema_version': 'constraint-repair-context.v1', 'sketch': sketch,
        'plan_hash': digest(before.model_dump(mode='json')),
        'checkpoint_hash': snapshot.get('checkpoint_hash'), 'diagnostic_hash': digest(snapshot),
        'snapshot': snapshot, 'construction_geometry': geometry,
        'planned_constraints': [{'logical_id': _logical_id(op), 'operation_id': op.op_id,
            'constraint': _constraint_args(op),
            'status': next(row['status'] for row in snapshot['operations'] if row['operation_id'] == op.op_id)}
            for op in before.operations if op.action == 'sketch.add_constraint' and op.args['sketch'] == sketch],
        'acceptance': acceptance, 'acceptance_hash': digest(acceptance), 'prior_contract': prior}


def _referenced(context, target):
    """Deletion may renumber constraints; conservatively protect all such references."""
    native = next(s for s in context['snapshot']['sketches'] if s['name'] == context['sketch'])
    row = next((c for c in native['constraints'] if c['logical_id'] == target), None)
    if row and row.get('name'):
        return 'named constraint/alias'
    if native.get('external_geometry'):
        return 'external geometry references'
    if context['snapshot'].get('references') or native.get('expressions'):
        return 'document expressions may reference native constraint ordinals or aliases'
    return None


def _constraint_args(operation):
    return {'driving': True, **{k: v for k, v in operation.args.items() if k not in {'sketch', 'logical_id'}}}


def requirement_protection(args, acceptance):
    """Conservative potential bindings cannot grant edit permission.

    The original operation contract has no semantic feature-role links. A numeric
    match is therefore only grounds to PROTECT, never evidence of requirement
    satisfaction or permission to remove another relationship. Native relation
    proofs, perturbations and final frozen engineering checks remain mandatory.
    """
    value = args.get('value_mm')
    if value is None:
        return []
    result = []
    for check in acceptance['checks']:
        if check['source_kind'] not in {'user', 'confirmed'}:
            continue
        if check['kind'] in {'solid_count', 'hole_count', 'void_connected', 'volume'}:
            continue
        values = [check['nominal']] if check.get('nominal') is not None else []
        if check['kind'] == 'hole_position':
            values.extend(v for point in check['scope']['centers_mm'] for v in point)
        if any(abs(value - v) <= TOLERANCE or
               args['kind'] == 'radius' and abs(2 * value - v) <= TOLERANCE for v in values):
            result.append(check['check_id'])
    return result


def compile_patch(before: FreeCADOperationPlan, proposal, context, *, max_changes=32):
    patch = ConstraintPatch.model_validate(proposal)
    sketch = context['sketch']
    for field in ('plan_hash', 'checkpoint_hash', 'diagnostic_hash', 'sketch'):
        if getattr(patch, field) != context[field]:
            raise PatchRejected('patch baseline/target does not match the diagnosed failure', sketch=sketch,
                                field=field, before=context[field], after=getattr(patch, field))
    if digest(before.model_dump(mode='json')) != context['plan_hash']:
        raise PatchRejected('source changed after diagnosis', sketch=sketch)
    if len(patch.changes) > max_changes:
        raise PatchRejected('patch exceeds the configured change budget', sketch=sketch)
    originals = { _logical_id(op): op for op in before.operations
        if op.action == 'sketch.add_constraint' and op.args['sketch'] == sketch }
    if len(originals) != sum(op.action == 'sketch.add_constraint' and op.args['sketch'] == sketch for op in before.operations):
        raise PatchRejected('source has ambiguous logical constraint identities', sketch=sketch)
    native = next(s for s in context['snapshot']['sketches'] if s['name'] == sketch)
    observed = {c['logical_id']: c for c in native['constraints']}
    geometry = context['construction_geometry']
    patch_hash = digest(patch.model_dump(mode='json'))
    replacements, additions, removed = {}, [], set()
    for change in patch.changes:
        identity = change.logical_id
        old = originals.get(identity)
        if change.action != 'add':
            if old is None:
                raise PatchRejected('unknown or committed constraint provenance is protected; committed operations cannot be rewritten',
                    sketch=sketch, logical_id=identity)
            if old.op_id in context['snapshot'].get('checkpoint_operations', {}):
                raise PatchRejected('committed checkpoint operation records are immutable', sketch=sketch, logical_id=identity)
            measured = observed.get(identity)
            if measured and (measured.get('operation_id') != old.op_id or measured.get('operation_hash') != digest(old.model_dump(mode='json'))):
                raise PatchRejected('logical constraint mapping does not match the source operation', sketch=sketch, logical_id=identity)
            protected_by = requirement_protection(old.args, context['acceptance'])
            if protected_by:
                raise PatchRejected('a saved user/confirmed requirement may depend on this driver; its binding stays protected',
                    sketch=sketch, logical_id=identity, field='requirement_links', before=protected_by,
                    after=change.model_dump(mode='json'))
        elif old is not None or identity in observed:
            raise PatchRejected('new logical constraint ID already exists', sketch=sketch, logical_id=identity)
        if change.action == 'delete':
            reason = _referenced(context, identity)
            if reason or any(op.action == 'sketch.set_constraint' and op.args['sketch'] == sketch for op in before.operations):
                raise PatchRejected('deletion would invalidate references: ' + (reason or 'indexed downstream constraint edit'),
                    sketch=sketch, logical_id=identity, before=old.model_dump(mode='json'))
            removed.add(old.op_id)
            continue
        args = change.constraint.model_dump(mode='json')
        rows = relation_rows(args, geometry)
        if rows is None or not all(satisfies(row, geometry) for row in rows):
            raise PatchRejected('proposed relation is not established by the frozen construction basis; do not invent dimensions or fix geometry',
                sketch=sketch, logical_id=identity, field='constraint', before=_constraint_args(old) if old else None, after=args)
        if old and _constraint_args(old) == args:
            raise PatchRejected('replacement makes no semantic progress', sketch=sketch, logical_id=identity)
        op_id = 'constraint-' + digest({'baseline': context['plan_hash'], 'patch': patch_hash,
                                      'logical_id': identity, 'constraint': args})[:48]
        operation = FreeCADOperation(op_id=op_id, action='sketch.add_constraint',
            args={'sketch': sketch, **args, 'logical_id': identity})
        if old:
            replacements[old.op_id] = operation
        else:
            additions.append(operation)
    target = [op for op in before.operations if op.action == 'sketch.add_constraint' and op.args['sketch'] == sketch]
    current = [replacements.get(op.op_id, op) for op in target if op.op_id not in removed] + additions
    # Include untouched checkpoint/primitive-generated relations in proofs, but they
    # remain protected: the Agent cannot claim ownership of their source.
    rules = [{'logical_id': _logical_id(op), 'args': _constraint_args(op), 'origin': 'persisted_plan'} for op in current]
    for row in native['constraints']:
        if row['logical_id'] not in originals:
            args = native_args(row)
            if args is not None:
                rules.append({'logical_id': row['logical_id'], 'args': {**args, 'driving': row.get('driving') is not False},
                              'origin': 'protected_checkpoint', 'native_index': row['index']})
    equations, equation_refs = [], []
    for rule in rules:
        rows = relation_rows(rule['args'], geometry)
        if rows is not None and all(satisfies(row, geometry) for row in rows) and rule['args'].get('driving', True):
            for index, row in enumerate(rows):
                equations.append(row); equation_refs.append({'logical_id': rule['logical_id'], 'row': index})
    previous = (context.get('prior_contract') or {}).get('sketches', {}).get(sketch, {})
    derivations, corrected = [], []
    for retained in previous.get('derivations', []):
        weights = derive_row(deserialize_row(retained['target']), equations)
        if weights is None:
            raise PatchRejected('patch loses a relationship established by an earlier repair', sketch=sketch,
                                logical_id=retained['logical_id'])
        derivations.append({**retained, 'supports': [{**ref, 'coefficient': str(weight)}
            for ref, weight in zip(equation_refs, weights) if weight]})
    for identity, old in originals.items():
        if old.op_id not in removed and old.op_id not in replacements:
            continue
        args = _constraint_args(old)
        rows = relation_rows(args, geometry)
        if rows is None:
            raise PatchRejected('unsupported necessary relationship stays protected', sketch=sketch, logical_id=identity)
        if not all(satisfies(row, geometry) for row in rows):
            # Only an incorrect endpoint/orientation relation in an uncommitted,
            # source-bound construction can be corrected from that construction.
            replacement = replacements.get(old.op_id)
            if args.get('value_mm') is not None or replacement is None or replacement.args['kind'] not in {'coincident', 'horizontal', 'vertical'}:
                raise PatchRejected('conflicting or unverified dimension requires clarification', sketch=sketch, logical_id=identity)
            corrected.append({'logical_id': identity, 'before': args, 'after': _constraint_args(replacement),
                              'basis': 'persisted_construction_geometry', 'source_operation_id': old.op_id})
            continue
        for row in rows:
            weights = derive_row(row, equations)
            if weights is None:
                raise PatchRejected('patch loses a required dimension or geometric relationship', sketch=sketch,
                    logical_id=identity, field='constraint', before=args,
                    after=_constraint_args(replacements[old.op_id]) if old.op_id in replacements else None)
            derivations.append({'logical_id': identity, 'target': serialize_row(row),
                'supports': [{**ref, 'coefficient': str(weight)} for ref, weight in zip(equation_refs, weights) if weight]})
    # New constraints must add information (or provide a reference measurement),
    # rather than merely changing an ID to defeat the no-progress detector.
    # A deleted/replaced equation must not make its replacement look redundant.
    # Incorrect source relations also cannot prove other relationships by
    # contradiction; only consistent, surviving equations count as evidence.
    surviving = [op for op in target if op.op_id not in removed and op.op_id not in replacements]
    surviving.extend(replacements.values())
    prior_rows = [row for op in surviving for row in (relation_rows(_constraint_args(op), geometry) or [])
                  if satisfies(row, geometry)]
    for op in additions:
        rows = relation_rows(_constraint_args(op), geometry) or []
        if op.args.get('driving', True) and rows and all(derive_row(row, prior_rows) is not None for row in rows):
            raise PatchRejected('added constraint is already implied; patch would make no progress', sketch=sketch,
                                logical_id=_logical_id(op), after=_constraint_args(op))
        prior_rows.extend(rows)
    operations = []
    insertion = max((i for i, op in enumerate(before.operations) if
        op.args.get('sketch') == sketch and op.action in {'sketch.add_constraint', 'sketch.add_geometry', 'sketch.add_profile'}), default=-1)
    if additions and any(i < insertion and (op.args.get('profile') == sketch or
            sketch in (op.args.get('profiles') or []) or op.args.get('path') == sketch)
            for i, op in enumerate(before.operations)):
        raise PatchRejected('constraint completion must precede its downstream consumers', sketch=sketch)
    if insertion < 0:
        # Additions to an existing checkpoint are append-only operations before
        # the first use; no saved ledger entry is modified or replayed as a change.
        operations.extend(additions)
    for index, op in enumerate(before.operations):
        if op.op_id not in removed:
            operations.append(replacements.get(op.op_id, op))
        if index == insertion:
            operations.extend(additions)
    after = FreeCADOperationPlan.model_validate({**before.model_dump(mode='json'),
                                               'operations': [op.model_dump(mode='json') for op in operations]})
    if after == before:
        raise PatchRejected('patch does not change the plan', sketch=sketch)
    certificate = deepcopy(context.get('prior_contract') or {})
    certificate.pop('execution_plan_hash', None)
    certificate.pop('execution_checkpoint_hash', None)
    history = certificate.get('patches', [])
    history.append({'patch': patch.model_dump(mode='json'), 'patch_hash': patch_hash,
        'source_plan': before.model_dump(mode='json'), 'diagnostic_hash': context['diagnostic_hash'],
        'context': {k: deepcopy(v) for k, v in context.items() if k != 'prior_contract'},
        'result_plan_hash': digest(after.model_dump(mode='json'))})
    certificate.update(schema_version='constraint-repair-contract.v1', acceptance=context['acceptance'],
        acceptance_hash=context['acceptance_hash'], checkpoint_hash=context['checkpoint_hash'],
        result_plan_hash=digest(after.model_dump(mode='json')), patches=history)
    certificate.setdefault('sketches', {})[sketch] = {'construction_geometry': geometry,
        'constraints': rules, 'derivations': derivations,
        'corrected_relations': previous.get('corrected_relations', []) + corrected,
        'diagnostic_hash': context['diagnostic_hash']}
    return after, certificate


def verify_compiled_contract(plan, certificate, acceptance):
    """Re-run scope and relationship proofs immediately before execution."""
    frozen = AcceptanceContract.model_validate(acceptance).model_dump(mode='json')
    if digest(frozen) != certificate['acceptance_hash']:
        raise PatchRejected('frozen acceptance changed after repair')
    prior = None
    for entry in certificate['patches']:
        source = FreeCADOperationPlan.model_validate(entry['source_plan'])
        context = {**entry['context'], 'prior_contract': prior or {}}
        result, prior = compile_patch(source, entry['patch'], context,
            max_changes=(entry['context'].get('resource_policy') or {}).get('max_changes', 32))
        if digest(result.model_dump(mode='json')) != entry['result_plan_hash']:
            raise PatchRejected('composed repair plan failed replay verification')
    if prior is None or prior['sketches'] != certificate['sketches']:
        raise PatchRejected('repair relationship obligations failed replay verification')
    expected = certificate.get('execution_plan_hash', certificate['result_plan_hash'])
    if digest(plan.model_dump(mode='json')) != expected:
        raise PatchRejected('execution plan does not match its repair proof')


def carry_contract(certificate, plan, checkpoint_hash):
    retained = deepcopy(certificate)
    retained['execution_plan_hash'] = digest(plan.model_dump(mode='json'))
    retained['execution_checkpoint_hash'] = checkpoint_hash
    return retained


def verify_native_receipt(metadata, certificate):
    """A successful process/shape alone cannot satisfy repaired-candidate gates."""
    import json
    native = metadata.get('result') or {}
    rows = [row for row in native.get('validations', []) if row.get('gate') == 'constraint_repair']
    if len(rows) != 1 or native.get('status') != 'succeeded':
        raise PatchRejected('native constraint repair validation report is missing')
    row = rows[0]
    report = json.loads(row.get('evidence_json') or '{}')
    if (row.get('status') != 'passed' or row.get('contract_hash') != digest(certificate)
            or report.get('status') != 'passed' or report.get('contract_hash') != digest(certificate)
            or report.get('schema_version') != 'constraint-repair-validation.v1'
            or {s['sketch'] for s in report.get('sketches', [])} != set(certificate['sketches'])
            or any(s.get('degrees_of_freedom') != 0 for s in report.get('sketches', []))
            or row.get('parameter_probes') != len(report.get('parameter_probes', []))
            or any(p.get('status') != 'passed' or p.get('downstream_recomputed') is not True
                   for p in report.get('parameter_probes', []))):
        raise PatchRejected('native constraint repair verification failed or belongs to another contract')
    return report


class NativeConstraintRepairValidation:
    validate_snapshot = staticmethod(validate_snapshot)
    verify_contract = staticmethod(verify_compiled_contract)
    verify_receipt = staticmethod(verify_native_receipt)


def progress_fingerprint(snapshot):
    """Compare physical/constraint state and execution progress, not renamed IDs."""
    def stable(value):
        if isinstance(value, float):
            return round(value, 7)
        if isinstance(value, list):
            return [stable(item) for item in value]
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items() if key not in {
                'logical_id', 'operation_id', 'operation_hash', 'plan_hash', 'generated_slot'}}
        return value
    return digest({'checkpoint': snapshot.get('checkpoint_hash'),
        'failure': snapshot['failure']['code'], 'sketches': stable(snapshot['sketches']),
        'progress': [row['status'] for row in snapshot['operations']]})
