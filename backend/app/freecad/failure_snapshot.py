"""Native failure evidence, separate from valid document/checkpoint projections.

Standard library only: the same implementation runs in FreeCAD's Python ABI.
Constraint ordinals are observations; logical IDs identify their generating operation.
"""
from __future__ import annotations

import hashlib
import json

try:
    from freecad_sketch_diagnostics import diagnose_sketch
except ModuleNotFoundError:
    from .sketch_diagnostics import diagnose_sketch


MAP_PROPERTY = '_AgentConstraintMap'
SNAPSHOT_SCHEMA = 'freecad-failure-snapshot.v1'


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def constraint_records(sketch):
    raw = getattr(sketch, MAP_PROPERTY, '[]')
    records = json.loads(raw)
    if not isinstance(records, list) or len({r['logical_id'] for r in records}) != len(records):
        raise ValueError('constraint identity mapping is invalid')
    return records


def write_constraint_records(sketch, records):
    if MAP_PROPERTY not in sketch.PropertiesList:
        sketch.addProperty('App::PropertyString', MAP_PROPERTY, 'CAD Agent')
        sketch.setEditorMode(MAP_PROPERTY, 2)
    setattr(sketch, MAP_PROPERTY, canonical(records))


def constraint_counts(document):
    return {o.Name: o.ConstraintCount for o in document.Objects if o.TypeId == 'Sketcher::SketchObject'}


def record_created_constraints(document, operation, counts, plan_hash):
    """Called inside the same native transaction, including a failing addition."""
    if operation['action'] not in {'sketch.add_constraint', 'sketch.add_profile'}:
        return
    sketch = document.getObject(operation['args']['sketch'])
    if sketch is None:
        return
    records = constraint_records(sketch)
    known = {r['index'] for r in records}
    start = counts.get(sketch.Name, 0)
    for index in range(start, sketch.ConstraintCount):
        if index in known:
            continue
        explicit = operation['action'] == 'sketch.add_constraint'
        logical_id = operation['args'].get('logical_id', operation['op_id']) if explicit else (
            'profile-' + digest({'operation': operation['op_id'], 'slot': index - start})[:40])
        records.append({'logical_id': logical_id, 'index': index,
            'operation_id': operation['op_id'], 'operation_hash': digest(operation),
            'plan_hash': plan_hash, 'origin': 'typed_operation',
            'generated_slot': index - start})
    write_constraint_records(sketch, records)


def geometry_snapshot(sketch):
    result = []
    for index, geometry in enumerate(sketch.Geometry):
        row = {'index': index, 'type': str(getattr(geometry, 'TypeId', type(geometry).__name__)),
               'construction': bool(sketch.getConstruction(index))}
        for attr, key in (('StartPoint', 'start'), ('EndPoint', 'end'), ('Center', 'center')):
            if hasattr(geometry, attr):
                row[key] = [float(v) for v in getattr(geometry, attr)]
        for attr, key in (('Radius', 'radius_mm'), ('MajorRadius', 'major_radius_mm'),
                          ('MinorRadius', 'minor_radius_mm'), ('FirstParameter', 'first_parameter'),
                          ('LastParameter', 'last_parameter')):
            if hasattr(geometry, attr):
                row[key] = float(getattr(geometry, attr))
        result.append(row)
    return result


def sketch_snapshot(sketch, checkpoint_hash):
    records = {r['index']: r for r in constraint_records(sketch)}
    constraints = []
    for index, constraint in enumerate(sketch.Constraints):
        row = {'index': index, 'type': str(constraint.Type), 'value': float(constraint.Value),
               'name': str(getattr(constraint, 'Name', ''))}
        for attr, key in (('First', 'first'), ('FirstPos', 'first_position'),
                          ('Second', 'second'), ('SecondPos', 'second_position'),
                          ('Third', 'third'), ('ThirdPos', 'third_position')):
            row[key] = int(getattr(constraint, attr, -1))
        try:
            row['driving'] = bool(sketch.getDriving(index))
        except (RuntimeError, ValueError):
            row['driving'] = None
        identity = records.get(index)
        row.update(identity or {'logical_id': 'unknown-' + digest({
            'checkpoint_hash': checkpoint_hash, 'sketch': sketch.Name, 'constraint': row})[:40],
            'origin': 'unknown', 'operation_id': None, 'plan_hash': None})
        constraints.append(row)
    return {'name': sketch.Name, 'geometry': geometry_snapshot(sketch),
        'constraints': constraints, 'solver': diagnose_sketch(sketch),
        'expressions': [list(e) for e in sketch.ExpressionEngine],
        'external_geometry': [[str(o.Name), list(sub)] for o, sub in sketch.ExternalGeometry],
        'placement': list(sketch.Placement.toMatrix().A)}


def capture_failure(document, plan, operation, completed, checkpoint_hash, base_ledger, error):
    sketches = [sketch_snapshot(o, checkpoint_hash) for o in document.Objects
                if o.TypeId == 'Sketcher::SketchObject']
    done = {row['op_id'] for row in completed}
    return {'schema_version': SNAPSHOT_SCHEMA, 'valid_checkpoint': False,
        'document_name': document.Name, 'checkpoint_hash': checkpoint_hash,
        'plan_hash': digest(plan), 'failed_operation_id': operation['op_id'],
        'failure': {'code': error.code, 'message': str(error), 'action': operation['action']},
        'operations': [{'operation_id': op['op_id'], 'operation_hash': digest(op),
            'status': 'executed' if op['op_id'] in done else 'failing' if op['op_id'] == operation['op_id'] else 'not_executed',
            'committed_checkpoint': op['op_id'] in base_ledger} for op in plan['operations']],
        'checkpoint_operations': base_ledger,
        'sketches': sketches,
        # Aliases and expressions may reference a constraint from ANY object.
        'references': [{'object': o.Name, 'expressions': [list(e) for e in o.ExpressionEngine]}
                       for o in document.Objects if getattr(o, 'ExpressionEngine', ())]}


def attach_failure_snapshot(error, document, plan, operation, completed, checkpoint_hash,
                            base_ledger, *, max_bytes=1024 * 1024):
    """Evidence is best effort. No extraction/serialization error may stop rollback."""
    if not (getattr(error, 'code', '').startswith('sketch_')
            or getattr(error, 'code', '') == 'constraint_repair_verification_failed'):
        return
    try:
        evidence = capture_failure(document, plan, operation, completed, checkpoint_hash, base_ledger, error)
        raw = canonical(evidence)
        if len(raw.encode()) > max_bytes:
            raise ValueError('failure snapshot exceeds diagnostic resource budget')
        error.details = {**error.details, 'failure_snapshot_json': raw,
            'failure_snapshot_sha256': hashlib.sha256(raw.encode()).hexdigest()}
    except Exception as exc:
        error.details = {**error.details, 'failure_snapshot_unavailable': type(exc).__name__}


def validate_snapshot(evidence, plan, checkpoint_hash, failed_operation_id):
    """Check the kernel observation against server-owned execution inputs."""
    if (evidence.get('schema_version') != SNAPSHOT_SCHEMA or evidence.get('valid_checkpoint') is not False
            or evidence.get('plan_hash') != digest(plan)
            or evidence.get('checkpoint_hash') != checkpoint_hash
            or evidence.get('failed_operation_id') != failed_operation_id):
        raise ValueError('failure diagnostic does not match the execution baseline')
    expected = {op['op_id']: digest(op) for op in plan['operations']}
    observed = evidence.get('operations', [])
    if len(observed) != len(expected) or {row['operation_id']: row['operation_hash'] for row in observed} != expected:
        raise ValueError('failure diagnostic operation inventory does not match the source')
    failing = [row['operation_id'] for row in observed if row['status'] == 'failing']
    if failing != [failed_operation_id]:
        raise ValueError('failure diagnostic must identify exactly one failing operation')
    cursor = next(i for i, op in enumerate(plan['operations']) if op['op_id'] == failed_operation_id)
    for index, (row, op) in enumerate(zip(observed, plan['operations'])):
        status = 'executed' if index < cursor else 'failing' if index == cursor else 'not_executed'
        if row['operation_id'] != op['op_id'] or row['status'] != status:
            raise ValueError('failure diagnostic execution progress is inconsistent')
    for sketch in evidence.get('sketches', []):
        ids = [c['logical_id'] for c in sketch['constraints']]
        if len(ids) != len(set(ids)) or len(sketch['geometry']) != len({g['index'] for g in sketch['geometry']}):
            raise ValueError('failure diagnostic has ambiguous native identities')
    return evidence
