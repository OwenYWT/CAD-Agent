"""Independent native checks for a backend-owned constraint repair contract."""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
import shutil
import tempfile

from freecad_failure_snapshot import canonical, constraint_records, digest, geometry_snapshot
from freecad_constraint_relationships import (
    TOLERANCE, deserialize_row, measured, relation_rows, satisfies, native_args,
)
from freecad_sketch_diagnostics import diagnose_sketch
from freecad_state_projector import project_parameters


class ConstraintVerificationError(RuntimeError):
    code = 'constraint_repair_verification_failed'

    def __init__(self, message, *, details=None, code=None):
        super().__init__(message)
        self.details = details or {}
        if code:
            self.code = code


def validate_baseline(plan, contract, checkpoint_hash, acceptance_hash):
    if (contract.get('schema_version') != 'constraint-repair-contract.v1'
            or contract.get('execution_plan_hash', contract.get('result_plan_hash')) != digest(plan)
            or contract.get('execution_checkpoint_hash', contract.get('checkpoint_hash')) != checkpoint_hash
            or contract.get('acceptance_hash') != acceptance_hash
            or digest(contract.get('acceptance')) != acceptance_hash):
        raise ConstraintVerificationError('repair contract baseline or frozen acceptance does not match')
    # Inspect each independently stored source/proposal pair. The current input
    # may be a later checkpoint continuation; that identity is separately bound above.
    for item in contract['patches']:
        patch, source = item['patch'], item['source_plan']
        if digest(patch) != item['patch_hash'] or digest(source) != patch['plan_hash']:
            raise ConstraintVerificationError('constraint patch history failed integrity verification')
        if any(change['action'] not in {'add', 'delete', 'replace'} for change in patch['changes']):
            raise ConstraintVerificationError('repair contains an unsupported mutation')


def _current_rules(sketch, specification):
    by_id = {row['logical_id']: row['index'] for row in constraint_records(sketch)}
    rules, equations, references = [], [], []
    for original in specification['constraints']:
        rule = deepcopy(original)
        index = by_id.get(rule['logical_id'], rule.get('native_index'))
        if index is None:
            raise ConstraintVerificationError(f"constraint identity is missing: {sketch.Name}/{rule['logical_id']}")
        args = rule['args']
        constraint = sketch.Constraints[index]
        actual_args = native_args({'type': constraint.Type, 'first': constraint.First,
            'first_position': constraint.FirstPos, 'second': constraint.Second,
            'second_position': constraint.SecondPos, 'value': constraint.Value})
        if actual_args is None or any(actual_args[key] != args.get(key) for key in ('kind', 'first', 'second')):
            raise ConstraintVerificationError('native constraint type or geometry references differ from the protected contract')
        if args.get('value_mm') is not None:
            args['value_mm'] = float(sketch.Constraints[index].Value)
            if bool(sketch.getDriving(index)) != bool(args.get('driving', True)):
                raise ConstraintVerificationError('driving/reference state differs from the protected contract')
        rows = relation_rows(args, specification['construction_geometry'])
        if rows is None:
            raise ConstraintVerificationError('native relationship proof is unavailable')
        rules.append((rule, index, rows))
        if args.get('driving', True):
            for row_index, row in enumerate(rows):
                equations.append(row); references.append((rule['logical_id'], row_index))
    return rules, equations, references


def validate_sketch(sketch, specification, *, nominal):
    diagnosis = diagnose_sketch(sketch)
    if diagnosis['constraint_status'] != 'fully_constrained':
        raise ConstraintVerificationError(f'{sketch.Name} is not completely and consistently constrained: {diagnosis}')
    actual = geometry_snapshot(sketch)
    if nominal:
        expected = specification['construction_geometry']
        if len(actual) != len(expected):
            raise ConstraintVerificationError('constraint repair changed sketch geometry count')
        for old, new in zip(expected, actual):
            if old['type'] != new['type'] or bool(old.get('construction')) != bool(new.get('construction')):
                raise ConstraintVerificationError('constraint repair changed geometry type or construction state')
            for field in ('start', 'end', 'center', 'radius_mm'):
                if field not in old:
                    continue
                a = old[field] if isinstance(old[field], list) else [old[field]]
                b = new.get(field)
                b = b if isinstance(b, list) else [b]
                if len(a) != len(b) or any(y is None or abs(x-y) > TOLERANCE for x, y in zip(a, b)):
                    raise ConstraintVerificationError(f'constraint repair changed declared construction geometry: {sketch.Name}/{old["index"]}/{field}')
    rules, equations, refs = _current_rules(sketch, specification)
    for rule, _, rows in rules:
        if not all(satisfies(row, actual) for row in rows):
            raise ConstraintVerificationError(f'constraint relation does not hold in native geometry: {rule["logical_id"]}')
    for proof in specification['derivations']:
        target = deserialize_row(proof['target'])
        value = Fraction(0)
        terms = {}
        for support in proof['supports']:
            key = (support['logical_id'], support['row'])
            if key not in refs:
                raise ConstraintVerificationError('derived relationship lost its supporting constraint')
            row = equations[refs.index(key)]
            weight = Fraction(support['coefficient'])
            value += weight * row['value']
            for variable, coefficient in row['terms'].items():
                terms[variable] = terms.get(variable, 0) + weight * coefficient
        if {k: v for k, v in terms.items() if v} != target['terms']:
            raise ConstraintVerificationError('relationship certificate is not mathematically valid')
        if nominal and abs(float(value - target['value'])) > TOLERANCE:
            raise ConstraintVerificationError('derived nominal dimension differs from its original requirement')
        if abs(float(measured(target, actual) - value)) > TOLERANCE:
            raise ConstraintVerificationError('parameter coupling does not hold in native geometry')
    return {'sketch': sketch.Name, 'degrees_of_freedom': diagnosis['degrees_of_freedom'],
            'checked_constraints': len(rules), 'derived_relationships': len(specification['derivations'])}


def validate_profile(sketch, *, closed):
    import Part
    shape = sketch.Shape
    if shape.isNull() or not shape.isValid() or not shape.Edges:
        raise ConstraintVerificationError(f'{sketch.Name} has no valid downstream profile')
    wires = list(shape.Wires)
    if not wires or sum(len(w.Edges) for w in wires) != len(shape.Edges):
        raise ConstraintVerificationError(f'{sketch.Name} profile contains disconnected edges')
    if closed and any(not wire.isClosed() for wire in wires):
        raise ConstraintVerificationError(f'{sketch.Name} requires closed profile wires')
    if closed:
        # A closed Sketcher wire can still cross itself. Pad may silently split
        # it into multiple solids; validate the actual planar region first.
        try:
            faces = [Part.Face(wire) for wire in wires]
            region = Part.makeFace(wires, 'Part::FaceMakerBullseye')
            for face in [*faces, region]:
                if not face.isValid():
                    raise ValueError('self-intersecting or invalid planar region')
                if face.check(True):
                    raise ValueError('planar region failed native topology checks')
        except Exception as exc:
            raise ConstraintVerificationError(
                f'{sketch.Name} profile cannot support a valid downstream feature: {exc}; '
                'constraint patches cannot change the frozen geometry', details={'object': sketch.Name}) from exc
    if not closed and len(wires) != 1:
        raise ConstraintVerificationError(f'{sketch.Name} sweep path must be connected')


def before_feature(document, operation, contract):
    args, action = operation['args'], operation['action']
    if not action.startswith('feature.'):
        return
    profiles = list(args.get('profiles') or []) + ([args['profile']] if args.get('profile') else [])
    for name in profiles + ([args['path']] if args.get('path') else []):
        sketch = document.getObject(name)
        if sketch is None or sketch.TypeId != 'Sketcher::SketchObject':
            continue
        if name in contract['sketches']:
            validate_sketch(sketch, contract['sketches'][name], nominal=True)
        validate_profile(sketch, closed=name != args.get('path'))


def _bodies(document):
    return {obj.Name: len(obj.Shape.Solids) for obj in document.Objects
            if obj.TypeId == 'PartDesign::Body' and not obj.Shape.isNull()}


def downstream_parameters(document, sketches):
    """Use the same editable-parameter contract as the product property editor."""
    affected = set(sketches)
    consumers = {}
    for obj in document.Objects:
        for dependency in getattr(obj, 'OutList', ()):
            consumers.setdefault(dependency.Name, set()).add(obj.Name)
    pending = list(affected)
    while pending:
        for name in consumers.get(pending.pop(), set()) - affected:
            affected.add(name)
            pending.append(name)
    return [{'object': item['object_name'], 'property': item['property_name'],
             'value': item['value'], 'unit': item['unit'], 'property_type': item['property_type']}
            for obj in document.Objects if obj.Name in affected
            for item in project_parameters(obj) if item['editable']]


def verify_document(document, contract, validate_document, *, max_probes=128, fraction=0.01):
    """Perturb isolated FCStd copies; no probe can mutate the accepted candidate."""
    import FreeCAD as App
    report = {'schema_version': 'constraint-repair-validation.v1', 'status': 'passed',
              'contract_hash': digest(contract), 'sketches': [], 'parameter_probes': []}
    parameters = []
    profile_modes = {}
    for name, specification in contract['sketches'].items():
        sketch = document.getObject(name)
        if sketch is None:
            raise ConstraintVerificationError('repaired sketch disappeared before acceptance')
        report['sketches'].append(validate_sketch(sketch, specification, nominal=True))
        profile_modes[name] = bool(sketch.Shape.Wires) and all(w.isClosed() for w in sketch.Shape.Wires)
        validate_profile(sketch, closed=profile_modes[name])
        by_id = {row['logical_id']: row['index'] for row in constraint_records(sketch)}
        for rule in specification['constraints']:
            if rule['args'].get('value_mm') is None or not rule['args'].get('driving', True):
                continue
            index = by_id.get(rule['logical_id'], rule.get('native_index'))
            if index is None:
                raise ConstraintVerificationError('editable parameter identity is unavailable')
            # Expressions are editable through their own source parameters. Do not
            # pretend an expression-controlled value is independently editable.
            if any(path in {f'Constraints[{index}]', 'Constraints.' + sketch.Constraints[index].Name}
                   for path, _ in sketch.ExpressionEngine):
                continue
            parameters.append({'object': name, 'constraint': index, 'logical_id': rule['logical_id'],
                               'value': float(sketch.Constraints[index].Value), 'kind': rule['args']['kind']})
    parameters.extend(downstream_parameters(document, contract['sketches']))
    if len(parameters) * 2 > max_probes:
        raise ConstraintVerificationError('parameter verification exceeds the configured native probe budget; not verified')
    if not 0 < fraction < 1:
        raise ConstraintVerificationError('invalid parameter perturbation policy')
    baseline_bodies = _bodies(document)
    with tempfile.TemporaryDirectory(prefix='constraint-parameter-probes-') as folder:
        original_path = Path(folder) / 'baseline.FCStd'
        # saveAs changes FileName, not geometry; probes always open a separate
        # physical file, as FreeCAD returns an existing document for the same path.
        document.saveAs(str(original_path))
        for index, parameter in enumerate(parameters):
            extent = max((abs(value) for specification in contract['sketches'].values()
                for g in specification['construction_geometry'] for key in ('start', 'end', 'center')
                for value in g.get(key, [])), default=1)
            delta = max(abs(parameter['value']) * fraction, extent * fraction if parameter['value'] == 0 else TOLERANCE * 100)
            integer = parameter.get('property_type') in {'App::PropertyInteger', 'App::PropertyIntegerConstraint'}
            if integer:
                delta = max(1, round(delta))
            for sign in (-1, 1):
                path = Path(folder) / f'probe-{index}-{sign}.FCStd'
                shutil.copyfile(original_path, path)
                probe = App.openDocument(str(path))
                try:
                    obj = probe.getObject(parameter['object'])
                    value = parameter['value'] + sign * delta
                    if 'constraint' in parameter:
                        obj.setDatum(parameter['constraint'], App.Units.Quantity(f'{value} mm'))
                    else:
                        requested = (f"{value} {parameter['unit']}" if parameter.get('unit') else
                                     int(value) if integer else value)
                        setattr(obj, parameter['property'], requested)
                        actual = getattr(obj, parameter['property'])
                        if parameter.get('unit'):
                            actual = actual.getValueAs(parameter['unit'])
                        if abs(float(getattr(actual, 'Value', actual)) - value) > TOLERANCE:
                            raise ConstraintVerificationError('native property clamped the parameter probe; not verified')
                    probe.recompute()
                    validate_document(probe, op_id='constraint-parameter-probe', action='constraint.verify')
                    for name, specification in contract['sketches'].items():
                        validate_sketch(probe.getObject(name), specification, nominal=False)
                        validate_profile(probe.getObject(name), closed=profile_modes[name])
                    if _bodies(probe) != baseline_bodies:
                        raise ConstraintVerificationError('parameter change breaks downstream solid connectivity')
                    report['parameter_probes'].append({**parameter, 'probe_value': value,
                                                       'status': 'passed', 'downstream_recomputed': True})
                except Exception as exc:
                    raise ConstraintVerificationError(
                        f'parameter coupling verification failed for {parameter["object"]}: {exc}',
                        code=('sketch_parameter_dependency_failed' if parameter['object'] in contract['sketches'] else None),
                        details={'object': parameter['object'], 'logical_id': parameter.get('logical_id', ''),
                            'property': parameter.get('property', ''), 'original_value': parameter['value'],
                            'probe_value': value, 'probe_failure': str(exc)[:2000]}) from exc
                finally:
                    App.closeDocument(probe.Name)
                    path.unlink(missing_ok=True)
    # Recheck the original after all probes; no isolated mutation may leak back.
    validate_document(document, op_id='constraint-repair-verified', action='constraint.verify')
    for name, specification in contract['sketches'].items():
        validate_sketch(document.getObject(name), specification, nominal=True)
    return report
