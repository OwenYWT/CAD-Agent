"""Read-only measurement provenance shared with the isolated geometry worker.

Bindings retain frozen world/bounds definitions or a server-confirmed object's
rigid local frame. Local authority also requires a hashed native artifact and
independent containment in the final STEP; model transforms are not authority.
"""
import hashlib
import json
import math
from itertools import product


def rigid_transform(matrix):
    if (len(matrix)!=16 or not all(math.isfinite(v) for v in matrix)
            or any(abs(a-b)>1e-9 for a,b in zip(matrix[12:],[0,0,0,1]))):
        raise ValueError('measurement transform is not finite affine geometry')
    rows=[matrix[i:i+3] for i in (0,4,8)]
    for i in range(3):
        for j in range(3):
            if abs(sum(a*b for a,b in zip(rows[i],rows[j]))-int(i==j))>1e-9:
                raise ValueError('measurement transform must be rigid')
    a,b,c=rows
    determinant=a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])
    if abs(determinant-1)>1e-9:
        raise ValueError('measurement transform must preserve orientation')
    return list(matrix)


def transform_scope(scope, matrix):
    """Exact rigid image, retaining an oriented region instead of widening it."""
    matrix=rigid_transform(matrix)
    def transform(point, direction=False):
        return [sum(matrix[i+j]*point[j] for j in range(3))+(0 if direction else matrix[i+3]) for i in (0,4,8)]
    result={**scope,'frame':'world'}
    if scope.get('axis') is not None:result['axis']=transform(scope['axis'],True)
    if scope.get('point_mm') is not None:result['point_mm']=transform(scope['point_mm'])
    result['centers_mm']=[transform(p) for p in scope.get('centers_mm',[])]
    if scope.get('region_min_mm') is not None:
        result['region_vertices_mm']=[transform(p) for p in product(*zip(scope['region_min_mm'],scope['region_max_mm']))]
        result.pop('region_min_mm');result.pop('region_max_mm')
        result['region_kind']='oriented_box'
    return result


def binding_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def bind_measurement_scope(check, *, acceptance_digest, context, artifact_sha256,
                           resolved_scope, frame_origin, evidence, native_binding=None):
    if context.get('schema_version') not in {'measurement-context.v1','measurement-context.v2'}:
        raise ValueError('unsupported measurement context')
    if artifact_sha256 not in context['artifact_sha256s']:
        raise ValueError('measurement binding artifact differs from its manifest')
    measured = bool(evidence['measured'])
    reason = evidence['details'].get('reason')
    status = ('bound' if measured else 'ambiguous' if reason == 'ambiguous_pair'
              else 'missing' if reason in {'no_pair','selected_object_missing'} or 'no_measurement_evidence' in evidence['issues']
              else 'unsupported')
    origin = list(frame_origin or (0., 0., 0.))
    result = {'schema_version': 'measurement-binding.v1', 'check_id': check['check_id'],
        'acceptance_digest': acceptance_digest, 'artifact_sha256': artifact_sha256,
        'base_revision_id': context['base_revision_id'], 'checkpoint_manifest_id': context['checkpoint_manifest_id'],
        'source_scope_digest': binding_digest(check.get('scope') or {}),
        'authority_ref': {'kind': 'frozen_acceptance_scope', 'acceptance_digest': acceptance_digest,
                          'check_id': check['check_id']},
        'frame_transform': [1.,0.,0.,origin[0],0.,1.,0.,origin[1],0.,0.,1.,origin[2],0.,0.,0.,1.],
        'resolved_scope': {**resolved_scope, 'frame': 'world'}, 'status': status,
        'proof': {'method': evidence['method'], 'coordinate_authority': (check.get('scope') or {}).get('frame','world'),
                  'selection': {k:v for k,v in evidence['details'].items() if k in
                      {'binding','face_indices','candidate_count','empty_intersections',
                       'reason','resolved_frame_origin_mm','axis','overlap_area_mm2',
                       'intervening_material_mm3','boolean_volume_tolerance_mm3'}},
                  'selection_uses_comparison_target': False}}
    reference=(check.get('scope') or {}).get('object_ref')
    if reference is not None:
        result['authority_ref']={'kind':'frozen_selection','object_ref':reference,
            'native_artifact_sha256':context['native_reference']['sha256']}
        if native_binding is None:
            if result['status']!='missing':result['status']='unsupported'
        else:
            if native_binding['reference']!=reference:
                raise ValueError('resolved object differs from the frozen selection')
            result['frame_transform']=rigid_transform(native_binding['frame_transform'])
            result['resolved_scope']=transform_scope(check['scope'],result['frame_transform'])
            result['proof']['native_component']=native_binding
    return {**result, 'binding_digest': binding_digest(result)}


def verify_measurement_binding(binding, *, check, acceptance_digest, context, artifact_sha256s):
    if not isinstance(binding, dict):
        raise ValueError('missing current measurement binding')
    unsigned = {k:v for k,v in binding.items() if k != 'binding_digest'}
    if binding.get('binding_digest') != binding_digest(unsigned):
        raise ValueError('measurement binding digest mismatch')
    expected = {'schema_version':'measurement-binding.v1', 'check_id':check['check_id'],
        'acceptance_digest':acceptance_digest, 'base_revision_id':context['base_revision_id'],
        'checkpoint_manifest_id':context['checkpoint_manifest_id'],
        'source_scope_digest':binding_digest(check.get('scope') or {}),
        'authority_ref':{'kind':'frozen_acceptance_scope','acceptance_digest':acceptance_digest,'check_id':check['check_id']}}
    reference=(check.get('scope') or {}).get('object_ref')
    if reference is not None:
        expected['authority_ref']={'kind':'frozen_selection','object_ref':reference,
            'native_artifact_sha256':context['native_reference']['sha256']}
        if reference not in context.get('selected_objects',[]):
            raise ValueError('measurement object was not admitted by the server')
        if binding.get('status')=='bound':
            native=binding.get('proof',{}).get('native_component',{})
            matrix=rigid_transform(native.get('frame_transform',[]))
            if (native.get('reference')!=reference or not native.get('contained_in_final_step')
                    or native.get('native_artifact_sha256')!=context['native_reference']['sha256']
                    or binding.get('frame_transform')!=matrix
                    or binding.get('resolved_scope')!=transform_scope(check['scope'],matrix)):
                raise ValueError('local measurement lacks exact coordinate equivalence evidence')
    if any(binding.get(k) != v for k,v in expected.items()):
        raise ValueError('measurement binding belongs to a different frozen request')
    if binding.get('artifact_sha256') not in set(artifact_sha256s) & set(context['artifact_sha256s']):
        raise ValueError('measurement binding belongs to a different artifact')
