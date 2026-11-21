"""Quantities recovered from a complete machined-feature certificate, shared with the CAD worker."""
import math


def principal_axis(axis):
    length = math.hypot(*axis)
    index = max(range(3), key=lambda i: abs(axis[i]))
    if not length or any(abs(axis[i])/length > 1e-9 for i in range(3) if i != index):
        raise ValueError('an axis parallel to a world coordinate axis is required')
    return index


def _numbers(value, size):
    return (isinstance(value, (list, tuple)) and len(value) == size
            and all(type(n) in (float, int) and math.isfinite(n) for n in value))


def _bounds(bounds):
    return _numbers(bounds, 6) and all(bounds[i] < bounds[i+3] for i in range(3))


def _same_axis(actual, wanted):
    n = math.hypot(*wanted)
    return (_numbers(actual, 3) and abs(math.hypot(*actual) - 1) <= 1e-9 and n > 0
            and min(math.dist(actual, [sign*v/n for v in wanted]) for sign in (-1,1)) <= 1e-9)


def feature_values(check, cert, tolerance):
    kind, scope = check['kind'], check['scope']
    bounds = cert.get('bounds_mm')
    if (cert.get('schema_version') != 'machined-feature.v1' or not _bounds(bounds)
            or cert.get('complete') is not True or cert.get('selection_uses_nominal') is not False
            or cert.get('symmetric_difference_solids') != [0, 0]
            or any(type(n) is not int for n in cert['symmetric_difference_solids'])
            or not _numbers([tolerance], 1) or not 0 < tolerance <= 1e-7):
        raise ValueError('machined feature lacks a complete material comparison')
    if kind == 'fillet_radius':
        radii = cert.get('radii_mm') or []
        requested = 4 if scope.get('axis') is not None else 12
        if (cert.get('family') != 'rounded_box' or cert.get('edge_count') != requested
                or len(radii) < requested or not _numbers(radii, len(radii))
                or min(radii) <= 0 or max(radii)-min(radii) > tolerance):
            raise ValueError('rounded edge coverage is incomplete')
        if scope.get('axis') is not None and cert.get('axis_index') != principal_axis(scope['axis']):
            raise ValueError('rounded edge family differs from scope')
        return [sum(radii)/len(radii)]
    if kind.startswith('rectangular_opening_'):
        axis = principal_axis(scope['axis'])
        openings = cert.get('openings_mm')
        if (cert.get('family') != 'rectangular_openings' or cert.get('axis_index') != axis
                or not isinstance(openings, list) or any(not _bounds(b) or
                   any(abs(bounds[i]-b[i]) > tolerance for i in (axis, axis+3)) or
                   any(b[i] <= bounds[i]+tolerance or b[i+3] >= bounds[i+3]-tolerance
                       for i in range(3) if i != axis) for b in openings)):
            raise ValueError('opening inventory is incomplete')
        if any(all(min(a[i+3], b[i+3]) > max(a[i], b[i]) for i in range(3))
               for j,a in enumerate(openings) for b in openings[:j]):
            raise ValueError('rectangular through-openings overlap')
        if kind == 'rectangular_opening_count':
            return [len(openings)]
        expected = [*scope['region_min_mm'], *scope['region_max_mm']]
        centers = [(a+b)/2 for a,b in zip(expected[:3], expected[3:])]
        matches = [b for b in openings if all(b[i] < v < b[i+3] for i,v in enumerate(centers))]
        if len(matches) != 1:
            raise ValueError('expected opening center does not identify one through-opening')
        return [abs(a-b) for a,b in zip(matches[0],expected)]
    cut_axis, origin, radius = (cert.get(k) for k in ('cut_axis','cut_origin_mm','cut_radius_mm'))
    if (cert.get('family') not in {'notched_plate','notched_tube'} or not _numbers(origin,3)
            or not _numbers(cut_axis,3) or abs(math.hypot(*cut_axis)-1)>1e-9
            or not _numbers([radius],1) or radius <= 0):
        raise ValueError('semicircular cut geometry is missing')
    if kind.startswith('semicircular_cut_'):
        if not _same_axis(cut_axis,scope['axis']):
            raise ValueError('cut axis differs from scope')
        if kind == 'semicircular_cut_diameter':
            return [2*radius]
        delta = [p-o for p,o in zip(scope['point_mm'],origin)]
        along = sum(a*v for a,v in zip(cut_axis,delta))
        return [math.hypot(*(v-along*a for v,a in zip(delta,cut_axis)))]
    outer, bore = cert.get('outer_radius_mm'), cert.get('bore_radius_mm')
    if (cert.get('family') != 'notched_tube' or not _same_axis(cert.get('tube_axis'),scope['axis'])
            or not _numbers([outer,bore],2) or not 0 < bore < outer):
        raise ValueError('the selected stock is not a complete cut tube')
    return [{'notched_tube_outer_diameter':2*outer, 'notched_tube_bore_diameter':2*bore,
             'notched_tube_wall_thickness':outer-bore}[kind]]
