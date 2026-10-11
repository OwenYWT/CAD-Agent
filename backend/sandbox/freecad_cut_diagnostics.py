"""Measured cut intersections and bounded, isolated previews of pending sketches.

Nothing in this module edits a production document or creates a checkpoint.
"""
from __future__ import annotations

import json

import FreeCAD as App
import Part

from freecad_sketch_diagnostics import diagnose_sketch


def vector(value):
    return [float(value.x), float(value.y), float(value.z)]


def bounds(shape):
    box = shape.optimalBoundingBox(False, False)
    return {'min': [float(box.XMin), float(box.YMin), float(box.ZMin)],
            'max': [float(box.XMax), float(box.YMax), float(box.ZMax)]}


def diagnose_cut(profile, base_shape, args, action):
    normal = profile.Placement.Rotation.multVec(App.Vector(0, 0, 1))
    report = {'schema_version': 'subtractive-evidence.v2', 'coordinate_space': 'body',
        'profile': profile.Name, 'origin': vector(profile.Placement.Base),
        'normal': vector(normal),
        'x_axis': vector(profile.Placement.Rotation.multVec(App.Vector(1, 0, 0))),
        'y_axis': vector(profile.Placement.Rotation.multVec(App.Vector(0, 1, 0))),
        'profile_bounds': bounds(profile.Shape), 'base_bounds': bounds(base_shape),
        'reversed': bool(args.get('reversed', False)), 'through_all': bool(args.get('through_all', False))}
    if action == 'feature.hole':
        if args.get('cut'):
            raise ValueError('a shaped hole needs its complete native cutting envelope')
        circles = [g for index, g in enumerate(profile.Geometry)
                   if not profile.getConstruction(index) and g.TypeId == 'Part::GeomCircle']
        if not circles:
            raise ValueError('hole has no circle profile')
        faces = [Part.Face(Part.Wire([Part.makeCircle(float(args['diameter_mm']) / 2,
            profile.Placement.multVec(g.Center), normal)])) for g in circles]
        region = Part.makeCompound(faces) if len(faces) > 1 else faces[0]
    elif action == 'feature.pocket':
        region = Part.makeFace(profile.Shape.Wires, 'Part::FaceMakerBullseye')
    else:
        raise ValueError('this operation requires a different cutting-envelope diagnostic')
    if region.isNull() or not region.isValid():
        raise ValueError('profile cannot produce a valid cutting region')
    reach = (base_shape.BoundBox.DiagonalLength + region.BoundBox.DiagonalLength
             + (region.BoundBox.Center - base_shape.BoundBox.Center).Length + 1)
    length = reach if report['through_all'] else float(args.get('length_mm') or args.get('depth_mm') or 0)
    if length <= 0:
        raise ValueError('cutting length is unavailable')
    direction = normal * (1 if report['reversed'] else -1)
    volume = lambda distance: float(base_shape.common(region.extrude(direction * distance)).Volume)
    actual, opposite = volume(length), volume(-length)
    through_actual, through_opposite = (actual, opposite) if report['through_all'] else (volume(reach), volume(-reach))
    tolerance = max(1e-7, abs(base_shape.Volume) * 1e-9)
    classification = ('intersects_material' if actual > tolerance else
        'wrong_direction' if opposite > tolerance else
        'insufficient_reach' if max(through_actual, through_opposite) > tolerance else
        'profile_outside_material')
    report.update(classification=classification, length_mm=length if not report['through_all'] else None,
        intersection_volume_mm3=actual, opposite_intersection_volume_mm3=opposite,
        through_intersection_volume_mm3=through_actual,
        through_opposite_intersection_volume_mm3=through_opposite)
    return report


def pending_profiles(document, plan, failed_operation, base_shape, dispatch, *, body_name, max_profiles=8, max_operations=64):
    """Preview construction only; later features can still change the target material."""
    operations = plan['operations']
    cursor = next(i for i, op in enumerate(operations) if op['op_id'] == failed_operation['op_id'])
    candidates, names = [], set()
    for index, op in enumerate(operations[cursor+1:], cursor+1):
        if op['action'] in {'feature.pocket', 'feature.hole'} and op['args']['profile'] not in names:
            names.add(op['args']['profile']); candidates.append((index, op))
    result = {'schema_version': 'pending-profile-diagnostics.v1', 'valid_checkpoint': False,
        'material_basis': failed_operation['op_id'], 'profiles': [],
        'truncated': len(candidates) > max_profiles, 'profile_budget': max_profiles}
    for index, consumer in candidates[:max_profiles]:
        name = consumer['args']['profile']
        row = {'sketch': name, 'consumer_operation_id': consumer['op_id'], 'status': 'unavailable'}
        temporary = None
        try:
            creates = [op for op in operations[:index] if op['action'] == 'sketch.create' and op['args']['name'] == name]
            if len(creates) != 1 or creates[0]['args'].get('body', 'Body') != body_name:
                raise ValueError('pending profile does not have the same privately constructed Body frame')
            start = operations.index(creates[0])
            construction = [op for op in operations[start+1:index]
                            if op['args'].get('sketch') == name or op['args'].get('object') == name]
            if len(construction) > max_operations:
                raise ValueError('pending construction exceeds diagnostic operation budget')
            if any(op['action'] not in {'sketch.add_geometry', 'sketch.add_profile', 'sketch.add_constraint'} for op in construction):
                raise ValueError('pending construction uses unsupported state-dependent operations')
            temporary = App.newDocument('PendingCutDiagnostic')
            dispatch['sketch.create'](temporary, creates[0]['args'])
            for op in construction:
                dispatch[op['action']](temporary, op['args'])
                temporary.recompute()
            profile = temporary.getObject(name)
            diagnosis = diagnose_sketch(profile)
            cut = diagnose_cut(profile, base_shape, consumer['args'], consumer['action'])
            row.update(status='diagnostic_only', coordinate_space='body',
                constraint_status=diagnosis['constraint_status'], degrees_of_freedom=diagnosis['degrees_of_freedom'],
                profile_bounds=cut['profile_bounds'], origin=cut['origin'], x_axis=cut['x_axis'],
                y_axis=cut['y_axis'], normal=cut['normal'],
                current_material_intersection_mm3=cut['intersection_volume_mm3'],
                opposite_current_material_intersection_mm3=cut['opposite_intersection_volume_mm3'],
                classification_against_current_material=cut['classification'])
        except Exception as error:
            row['reason'] = str(error)[:500]
        finally:
            if temporary is not None:
                App.closeDocument(temporary.Name)
                App.setActiveDocument(document.Name)
        result['profiles'].append(row)
    return json.dumps(result, ensure_ascii=False, allow_nan=False)
