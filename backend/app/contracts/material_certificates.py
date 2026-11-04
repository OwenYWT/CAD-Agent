"""Receiver checks for whole-boundary material measurements, without CAD imports."""
import math


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def verify_material_certificate(check, evidence):
    kind, details = check['kind'], evidence['details']
    if evidence['outcome'] == 'indeterminate':
        return
    key = None
    if kind.startswith(('cylinder_', 'sphere_')):
        key = 'material_primitive'
        cert = details.get(key) or {}
        primitive, quantity = kind.split('_', 1)
        count = cert.get('face_count')
        dimensions = cert.get('dimensions_mm') or {}
        allowed = {'CYLINDER','PLANE'} if primitive == 'cylinder' else {'SPHERE'}
        if (cert.get('schema_version') != 'material-primitive.v1' or cert.get('primitive') != primitive
                or cert.get('solid_count') != 1 or type(count) is not int or count < 1
                or cert.get('covered_face_indices') != list(range(count))
                or len(cert.get('face_types', [])) != count or not set(cert['face_types']) <= allowed
                or set(dimensions) != ({'radius','diameter','height'} if primitive == 'cylinder' else {'radius','diameter'})
                or not all(_positive(v) for v in dimensions.values())
                or dimensions['diameter'] != 2*dimensions['radius']
                or list(evidence['measured']) != [dimensions[quantity]]):
            raise ValueError('material dimensions lack complete primitive boundary evidence')
        if primitive == 'cylinder':
            geometry = cert.get('geometry') or {}
            interval, axis = geometry.get('axial_interval_mm', []), geometry.get('axis', [])
            if (set(cert['face_types']) != allowed or len(interval) != 2 or len(axis) != 3
                    or not all(isinstance(v, (float,int)) and math.isfinite(v) for v in [*axis,*interval])
                    or abs(math.hypot(*axis)-1) > 1e-10 or interval[1]-interval[0] != dimensions['height']):
                raise ValueError('cylinder dimensions disagree with its complete boundary')
    elif kind == 'wall_thickness' and check.get('scope', {}).get('wall_mode') in {'side_walls','cavity_floor'}:
        key = 'cavity_walls'
        cert = details.get(key) or {}
        mode = check['scope']['wall_mode']
        sides, floor = cert.get('side_face_indices', []), cert.get('floor_face_index')
        selected = sides if mode == 'side_walls' else [floor]
        layers = cert.get('layers', [])
        if (cert.get('schema_version') != 'cavity-walls.v1' or cert.get('mode') != mode
                or not _positive(cert.get('cavity_height_mm'))
                or type(floor) is not int or floor < 0 or len(sides) != 4
                or any(type(i) is not int or i < 0 for i in sides) or len(set([floor,*sides])) != 5
                or [r.get('face_index') for r in layers] != selected
                or any(r.get('material_complete') is not True or r.get('complete_face') is not True
                       or type(r.get('inner_boundary_count')) is not int or r['inner_boundary_count'] != 0
                       or not _positive(r.get('certified_area_mm2'))
                       or not _positive(r.get('thickness_mm')) for r in layers)
                or list(evidence['measured']) != [r['thickness_mm'] for r in layers]):
            raise ValueError('wall thickness lacks all required opposed material layers')
    if key is not None:
        if cert.get('complete') is not True or cert.get('selection_uses_nominal') is not False:
            raise ValueError('material measurement coverage is incomplete or target-dependent')
        binding = details.get('measurement_binding')
        if binding is not None and binding.get('proof', {}).get('selection', {}).get(key) != cert:
            raise ValueError('material certificate differs from its artifact-bound measurement')
