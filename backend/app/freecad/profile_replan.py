"""Backend-owned scope for rebuilding an invalid, uncommitted sketch boundary.

This is deliberately separate from constraint patches. A topology failure does
not grant permission to change an accepted checkpoint, feature or requirement.
"""
from copy import deepcopy
import json

from app.contracts.acceptance import AcceptanceContract
from app.freecad.constraint_patch import construction_geometry
from app.freecad.contracts import FreeCADOperationPlan, FreeCADPlanningError
from app.freecad.failure_snapshot import digest, validate_snapshot
from app.freecad.selection import validate_selected_operations


PROFILE_ERROR = 'profile_geometry_invalid'
_EDITABLE = {'sketch.add_geometry', 'sketch.add_profile', 'sketch.add_constraint'}


def rejected(reason):
    return FreeCADPlanningError('profile_replan_rejected', reason)


def _geometry(plan, sketch):
    try:
        geometry = construction_geometry(plan, sketch, None)
    except ValueError as exc:
        raise rejected('profile replan requires a supported, explicit construction basis') from exc
    for item in geometry:
        if item['type'] == 'Part::GeomLineSegment' and all(
                abs(item['start'][i] - item['end'][i]) > 1e-7 for i in (0, 1)):
            raise rejected('non-axis linear relationship proofs are not available; geometry remains protected')
    return geometry


def _bounds(geometry):
    points = []
    for item in geometry:
        if item.get('construction'):
            continue
        if item['type'] == 'Part::GeomLineSegment':
            points.extend([item['start'], item['end']])
        elif item['type'] == 'Part::GeomCircle':
            points.extend([[c + sign * item['radius_mm'] for c in item['center'][:2]]
                           for sign in (-1, 1)])
        else:
            raise rejected('unsupported curves remain protected')
    if not points:
        raise rejected('profile has no material boundary')
    return [min(p[i] for p in points) for i in (0, 1)] + [max(p[i] for p in points) for i in (0, 1)]


def build_context(before, snapshot, failure, base_state=None, prior_contract=None):
    validate_snapshot(snapshot, before.model_dump(mode='json'), snapshot.get('checkpoint_hash'), failure['operation_id'])
    observed = snapshot['failure']
    details = observed.get('details') or {}
    if (failure.get('error_code') != PROFILE_ERROR or observed['code'] != PROFILE_ERROR
            or details.get('stage') != 'profile_topology'
            or not isinstance(details.get('closed_profile'), bool)):
        raise rejected('a verified native profile-topology failure is required')
    sketch = details.get('object')
    target = next((op for op in before.operations if op.op_id == failure['operation_id']), None)
    if target is None or target.action not in {'feature.pad', 'feature.pocket', 'feature.hole',
            'feature.loft', 'feature.sweep', 'feature.revolve'}:
        raise rejected('failure is not a supported profile consumer')
    profiles = [target.args.get('profile'), target.args.get('path'), *target.args.get('profiles', [])]
    if sketch not in profiles or observed['action'] != target.action:
        raise rejected('diagnosed sketch does not belong to the failed feature')
    creation = next((op for op in before.operations if op.action == 'sketch.create' and op.args['name'] == sketch), None)
    base_names = {o['name'] for o in (base_state or {}).get('objects', [])}
    ledger = snapshot.get('checkpoint_operations', {})
    if creation is None or creation.op_id in ledger or sketch in base_names:
        raise rejected('accepted checkpoint and base-document sketches cannot be replanned')
    native = next((s for s in snapshot['sketches'] if s['name'] == sketch), None)
    if native is None or native.get('external_geometry') or native.get('expressions') or snapshot.get('references'):
        raise rejected('external references and expressions remain protected')
    if sketch in (prior_contract or {}).get('sketches', {}):
        raise rejected('an earlier constraint proof protects this geometry; its obligations cannot be discarded')
    if any(op.action == 'api.execute' or op.action == 'sketch.set_constraint' and op.args['sketch'] == sketch
           for op in before.operations):
        raise rejected('native programs and indexed constraint edits cannot be rewritten by profile replan')
    acceptance = AcceptanceContract.model_validate(failure.get('engineering_acceptance') or {})
    required = [c for c in acceptance.checks if c.required]
    if (acceptance.unresolved or acceptance.verification_limits or not required
            or not any(c.kind not in {'solid_count', 'volume'} for c in required)):
        raise rejected('complete frozen dimensional acceptance is required for profile replan')
    if any(c.get('origin') == 'unknown' for c in native['constraints']):
        raise rejected('unknown constraint provenance remains protected')
    geometry = _geometry(before, sketch)
    return {'schema_version': 'profile-replan-context.v1', 'sketch': sketch,
        'closed_profile': details['closed_profile'], 'source_plan': before.model_dump(mode='json'),
        'snapshot': snapshot, 'failure': deepcopy(failure), 'base_state': deepcopy(base_state),
        'prior_constraint_contract': deepcopy(prior_contract), 'bounds': _bounds(geometry),
        'checkpoint_hash': snapshot.get('checkpoint_hash'),
        'acceptance': acceptance.model_dump(mode='json'),
        'acceptance_hash': digest(acceptance.model_dump(mode='json'))}


def validate_proposal(after, context):
    before = FreeCADOperationPlan.model_validate(context['source_plan'])
    sketch = context['sketch']
    editable = lambda op: op.action in _EDITABLE and op.args.get('sketch') == sketch
    if (before.document_name != after.document_name or before.execution_mode != after.execution_mode
            or [op for op in before.operations if not editable(op)] != [op for op in after.operations if not editable(op)]):
        raise rejected('profile replan must preserve frames, features, exports and all unrelated operations')
    ledger = context['snapshot'].get('checkpoint_operations', {})
    for op in after.operations:
        if op.op_id in ledger and digest(op.model_dump(mode='json')) != ledger[op.op_id]:
            raise rejected('checkpoint operation identities are immutable')
    old = [op for op in before.operations if editable(op)]
    new = [op for op in after.operations if editable(op)]
    # An ID-only rewrite is not progress and must not spend a CAD attempt.
    semantic = lambda ops: [{'action': op.action, 'args': op.args} for op in ops]
    if semantic(old) == semantic(new):
        raise rejected('profile replan made no geometric or constraint change')
    geometry = _geometry(after, sketch)
    if any(abs(a - b) > 1e-7 for a, b in zip(_bounds(geometry), context['bounds'])):
        raise rejected('profile replan cannot resize the original construction envelope')
    cursor = next(i for i, op in enumerate(after.operations) if op.op_id == context['failure']['operation_id'])
    if any(editable(op) for op in after.operations[cursor:]):
        raise rejected('profile construction must finish before its failed consumer')
    if context.get('base_state'):
        validate_selected_operations(after, context['base_state'])
    return geometry


def make_contract(after, context, policy):
    geometry = validate_proposal(after, context)
    return {'schema_version': 'profile-replan-contract.v1', 'context': deepcopy(context),
        'result_plan': after.model_dump(mode='json'), 'execution_plan_hash': digest(after.model_dump(mode='json')),
        'execution_checkpoint_hash': context['checkpoint_hash'], 'acceptance_hash': context['acceptance_hash'],
        'sketch': context['sketch'], 'closed_profile': context['closed_profile'],
        'construction_geometry': geometry, 'resource_policy': dict(policy)}


def carry_contract(contract, plan, checkpoint_hash):
    return {**deepcopy(contract), 'execution_plan_hash': digest(plan.model_dump(mode='json')),
            'execution_checkpoint_hash': checkpoint_hash}


def verify_contract(plan, contract, acceptance, checkpoint_hash):
    context = contract['context']
    rebuilt = build_context(FreeCADOperationPlan.model_validate(context['source_plan']), context['snapshot'],
        context['failure'], context.get('base_state'), context.get('prior_constraint_contract'))
    expected = make_contract(FreeCADOperationPlan.model_validate(contract['result_plan']), rebuilt, contract['resource_policy'])
    if any(expected[k] != contract[k] for k in ('schema_version', 'context', 'sketch', 'closed_profile',
            'construction_geometry', 'acceptance_hash')):
        raise rejected('profile replan proof failed independent reconstruction')
    frozen = AcceptanceContract.model_validate(acceptance).model_dump(mode='json')
    if (digest(frozen) != contract['acceptance_hash'] or digest(plan.model_dump(mode='json')) != contract['execution_plan_hash']
            or checkpoint_hash != contract['execution_checkpoint_hash']):
        raise rejected('profile replan source, checkpoint or frozen acceptance changed')


def progress_fingerprint(snapshot):
    details = snapshot['failure'].get('details') or {}
    sketch = next((s for s in snapshot['sketches'] if s['name'] == details.get('object')), {})
    geometry = []
    for item in sketch.get('geometry', []):
        stable = {k: v for k, v in item.items() if k != 'index'}
        if 'start' in stable and 'end' in stable:
            stable['start'], stable['end'] = sorted([stable['start'], stable['end']])
        geometry.append(stable)
    geometry.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return 'profile:' + digest({'geometry': geometry, 'placement': sketch.get('placement'),
        'reason': details.get('reason'), 'action': snapshot['failure']['action']})


def verify_receipt(metadata, contract):
    native = metadata.get('result') or {}
    rows = [r for r in native.get('validations', []) if r.get('gate') == 'profile_replan']
    if len(rows) != 1 or native.get('status') != 'succeeded':
        raise rejected('native profile replan evidence is missing')
    row = rows[0]
    report = json.loads(row.get('evidence_json') or '{}')
    if (row.get('status') != 'passed' or report.get('status') != 'passed'
            or report.get('schema_version') != 'profile-replan-validation.v1'
            or row.get('contract_hash') != digest(contract) or report.get('contract_hash') != digest(contract)
            or report.get('sketch') != contract['sketch'] or not report.get('parameter_probes')
            or report.get('saved_reopened') is not True
            or any(p.get('status') != 'passed' for p in report['parameter_probes'])):
        raise rejected('native profile replan evidence is incomplete or belongs to another contract')
    return report
