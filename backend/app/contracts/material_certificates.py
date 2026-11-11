"""Receiver checks for whole-boundary material measurements, without CAD imports."""
import math


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def verify_material_certificate(check, evidence):
    kind, details = check['kind'], evidence['details']
    if evidence['outcome'] == 'indeterminate':
        return
    key = None
    if kind.startswith('annular_segment_'):
        key='annular_segment';cert=details.get(key) or {}
        dimensions=cert.get('dimensions_mm') or {}
        axis,point,origin,interval=(cert.get(k,[]) for k in
            ('axis','axis_point_mm','origin_mm','axial_interval_mm'))
        tolerance=details.get('numerical_tolerance_mm')
        if (cert.get('schema_version')!='annular-segment.v1'
                or set(dimensions)!={'diameter','inner_diameter','height'}
                or not all(_positive(v) for v in dimensions.values())
                or len(axis)!=3 or len(point)!=3 or len(origin)!=3 or len(interval)!=2
                or not _positive(tolerance) or tolerance>1e-7
                or type(cert.get('outer_face_index')) is not int or cert['outer_face_index']<0
                or not all(isinstance(v,(float,int)) and not isinstance(v,bool) and math.isfinite(v)
                    for v in [*axis,*point,*origin,*interval,
                        cert.get('outer_surface_area_mm2'),cert.get('missing_material_mm3'),
                        cert.get('excess_material_mm3'),cert.get('boolean_volume_tolerance_mm3')])):
            raise ValueError('annular segment lacks complete measured material evidence')
        outer,inner,height=dimensions['diameter']/2,dimensions['inner_diameter']/2,dimensions['height']
        requested=check['scope'];direction=requested['axis'];length=math.hypot(*direction)
        along=sum(a*p for a,p in zip(axis,point))
        volume_tolerance=cert['boolean_volume_tolerance_mm3']
        expected_tolerance=(2*math.pi*(outer**2-inner**2)+2*math.pi*(outer+inner)*height)*tolerance
        if (outer<=inner or abs(math.hypot(*axis)-1)>1e-10
                or any(abs(a-b/length)>1e-10 for a,b in zip(axis,direction))
                or point!=list(requested['point_mm'])
                or math.hypot(*(p-along*a-o for p,a,o in zip(point,axis,origin)))>tolerance
                or not interval[0]+tolerance<along<interval[1]-tolerance
                or interval[1]-interval[0]!=height
                or abs(cert['outer_surface_area_mm2']-2*math.pi*outer*height)>tolerance*(4*math.pi*outer+2*height)
                or not math.isclose(volume_tolerance,expected_tolerance,rel_tol=1e-8,abs_tol=1e-12)
                or not 0<=cert['missing_material_mm3']<=volume_tolerance
                or not 0<=cert['excess_material_mm3']<=volume_tolerance
                or list(evidence['measured'])!=[dimensions[kind.removeprefix('annular_segment_')]]):
            raise ValueError('annular segment dimensions, selection or complete material differ')
    elif kind.startswith(('cylinder_', 'sphere_', 'cube_')):
        key = 'material_primitive'
        cert = details.get(key) or {}
        primitive, quantity = kind.split('_', 1)
        count = cert.get('face_count')
        dimensions = cert.get('dimensions_mm') or {}
        allowed = {'cylinder': {'CYLINDER','PLANE'}, 'sphere': {'SPHERE'}, 'cube': {'PLANE'}}[primitive]
        dimension_keys = {'cylinder': {'radius','diameter','height'}, 'sphere': {'radius','diameter'}, 'cube': {'edge'}}[primitive]
        if (cert.get('schema_version') != 'material-primitive.v1' or cert.get('primitive') != primitive
                or cert.get('solid_count') != 1 or type(count) is not int or count < 1
                or cert.get('covered_face_indices') != list(range(count))
                or len(cert.get('face_types', [])) != count or not set(cert['face_types']) <= allowed
                or set(dimensions) != dimension_keys
                or not all(_positive(v) for v in dimensions.values())
                or primitive != 'cube' and dimensions['diameter'] != 2*dimensions['radius']
                or list(evidence['measured']) != ([1.] if quantity == 'shape' else [dimensions[quantity]])):
            raise ValueError('material dimensions lack complete primitive boundary evidence')
        if primitive == 'cylinder':
            geometry = cert.get('geometry') or {}
            interval, axis = geometry.get('axial_interval_mm', []), geometry.get('axis', [])
            if (set(cert['face_types']) != allowed or len(interval) != 2 or len(axis) != 3
                    or not all(isinstance(v, (float,int)) and math.isfinite(v) for v in [*axis,*interval])
                    or abs(math.hypot(*axis)-1) > 1e-10 or interval[1]-interval[0] != dimensions['height']):
                raise ValueError('cylinder dimensions disagree with its complete boundary')
        if primitive == 'cube':
            geometry = cert.get('geometry') or {}
            axes, intervals, pairs = (geometry.get(k, []) for k in ('axes', 'intervals_mm', 'face_axis_pairs'))
            tolerance = details.get('numerical_tolerance_mm')
            if (count != 6 or geometry.get('edge_count') != 12 or geometry.get('vertex_count') != 8
                    or not _positive(tolerance) or tolerance > 1e-7
                    or geometry.get('inner_boundary_counts') != [0]*6
                    or geometry.get('boundary_edge_counts') != [4]*6
                    or len(axes) != 3 or len(intervals) != 3 or len(pairs) != 3
                    or any(not isinstance(a, list) or len(a) != 3 for a in axes)
                    or any(not isinstance(p, list) or len(p) != 2 for p in intervals + pairs)
                    or any(type(i) is not int for pair in pairs for i in pair)
                    or sorted(i for pair in pairs for i in pair) != list(range(6))
                    or any(not isinstance(v, (float, int)) or isinstance(v, bool) or not math.isfinite(v)
                           for row in axes + intervals for v in row)):
                raise ValueError('cube evidence lacks its complete opposed planar boundary')
            lengths = [hi-lo for lo, hi in intervals]
            if (any(abs(math.hypot(*axis)-1) > 1e-10 for axis in axes)
                    or any(abs(sum(a*b for a,b in zip(axes[i], axes[j]))) > 1e-10
                           for i in range(3) for j in range(i))
                    or min(lengths) <= 0 or max(lengths)-min(lengths) > tolerance
                    or dimensions['edge'] != sum(lengths)/3):
                raise ValueError('cube edges disagree with its orthogonal measured boundary')
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
