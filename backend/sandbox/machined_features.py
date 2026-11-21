"""Independent measurements of machined solids, reconstructed from the final STEP."""
import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Surface

try:
    from feature_verification import _geometric_bounds
    from machined_feature_values import principal_axis, feature_values
except ModuleNotFoundError:
    from sandbox.feature_verification import _geometric_bounds
    from app.contracts.machined_feature_values import principal_axis, feature_values


def _box(bounds):
    return cq.Solid.makeBox(*(b-a for a, b in zip(bounds[:3], bounds[3:])), cq.Vector(*bounds[:3]))


def _same_material(actual, reconstructed):
    if not reconstructed.isValid() or len(reconstructed.Solids()) != 1:
        raise ValueError('feature reconstruction did not produce a valid solid')
    counts = []
    for left, right in ((actual, reconstructed), (reconstructed, actual)):
        difference = left.cut(right)
        counts.append(len(difference.Solids()))
        if not difference.isValid() or counts[-1]:
            raise ValueError('the reconstructed feature leaves missing or excess material')
    return counts


def _cylinder(face):
    surface = BRepAdaptor_Surface(face.wrapped, True).Cylinder()
    axis = cq.Vector(*surface.Axis().Direction().Coord())
    origin = cq.Vector(*surface.Location().Coord())
    # Orientation of the topological face determines material side even on a trimmed support.
    return axis, origin - axis * origin.dot(axis), surface.Radius(), face.wrapped.Orientation().name == 'TopAbs_REVERSED'


def _parallel(a, b):
    return a.cross(b).Length <= 1e-9


def _rounded_box(shape, bounds, scope, tolerance):
    cylinders = [_cylinder(f) for f in shape.Faces() if f.geomType() == 'CYLINDER']
    if not cylinders or any(reversed_face for _, _, _, reversed_face in cylinders):
        raise ValueError('no complete set of convex edge blends')
    radii = [r for _, _, r, _ in cylinders]
    if max(radii)-min(radii) > tolerance:
        raise ValueError('edge blends do not have a common radius')
    radius = sum(radii)/len(radii)
    block = _box(bounds)
    axis = cq.Vector(*scope['axis']).normalized() if scope.get('axis') is not None else None
    if axis is not None:
        principal_axis(scope['axis'])
    edges = [e for e in block.Edges() if axis is None or _parallel(e.tangentAt(), axis)]
    if len(edges) != (12 if axis is None else 4):
        raise ValueError('the requested edge family is ambiguous')
    difference = _same_material(shape, block.fillet(radius, edges))
    return {'family': 'rounded_box', 'radii_mm': radii, 'edge_count': len(edges),
            'symmetric_difference_solids': difference,
            'axis_index': principal_axis(scope['axis']) if axis is not None else None}


def _openings(shape, bounds, scope, tolerance):
    axis = principal_axis(scope['axis'])
    blank = _box(bounds)
    void = blank.cut(shape)
    if not void.isValid():
        raise ValueError('invalid complementary opening geometry')
    openings = []
    boxes = []
    for solid in void.Solids():
        opening = list(_geometric_bounds(solid))
        if (any(abs(opening[k] - bounds[k]) > tolerance for k in (axis, axis+3))
                or any(opening[k] <= bounds[k]+tolerance or opening[k+3] >= bounds[k+3]-tolerance
                       for k in range(3) if k != axis)):
            raise ValueError('the plate contains a blind pocket or an open edge')
        box = _box(opening)
        _same_material(solid, box)
        # A through cutter spans both stock faces, even when a measured end differs by roundoff.
        cutter_bounds = opening.copy()
        cutter_bounds[axis], cutter_bounds[axis+3] = bounds[axis], bounds[axis+3]
        boxes.append(_box(cutter_bounds))
        openings.append(opening)
    difference = _same_material(shape, blank.cut(*boxes) if boxes else blank)
    return {'family': 'rectangular_openings', 'axis_index': axis, 'openings_mm': sorted(openings),
            'symmetric_difference_solids': difference}


def _semicircular_cut(shape, bounds, tolerance):
    cylinders = []
    planes = []
    for face in shape.Faces():
        if face.geomType() == 'PLANE':
            planes.append((face.Center(), face.normalAt()))
            continue
        if face.geomType() != 'CYLINDER':
            raise ValueError('cut stock contains an unaccounted curved surface')
        axis, origin, radius, concave = _cylinder(face)
        if not any(_parallel(axis, a) and (origin-o).Length <= tolerance and abs(radius-r) <= tolerance and c == concave
                   for a, o, r, c in cylinders):
            cylinders.append((axis, origin, radius, concave))
    outside = [c for c in cylinders if not c[3]]
    inside = [c for c in cylinders if c[3]]
    span = shape.BoundingBox().DiagonalLength
    if not outside:
        if len(inside) != 1:
            raise ValueError('expected one circular through-notch')
        axis, center, radius, _ = inside[0]
        direction = principal_axis(axis.toTuple())
        transverse = [i for i in range(3) if i != direction]
        distances = [sorted((center.toTuple()[i]-bounds[i], bounds[i+3]-center.toTuple()[i])) for i in transverse]
        if (sum(abs(near) <= tolerance for near, far in distances) != 1
                or any(far < radius-tolerance or (abs(near)>tolerance and near < radius-tolerance)
                       for near, far in distances)):
            raise ValueError('the notch must cut one whole semicircle on one edge')
        blank = _box(bounds)
        result = {'family': 'notched_plate'}
    else:
        if len(outside) != 1 or len(inside) != 2:
            raise ValueError('expected one tube and one transverse cut')
        tube_axis, tube_origin, outer, _ = outside[0]
        bores = [c for c in inside if _parallel(c[0], tube_axis) and (c[1]-tube_origin).Length <= tolerance]
        if len(bores) != 1 or bores[0][2] >= outer-tolerance:
            raise ValueError('tube bore is not coaxial')
        bore = bores[0][2]
        axis, center, radius, _ = next(c for c in inside if c is not bores[0])
        if abs(tube_axis.dot(axis)) > 1e-9:
            raise ValueError('tube cut is not transverse')
        ends = sorted((p-tube_origin).dot(tube_axis) for p, n in planes if _parallel(n,tube_axis))
        if len(ends) != 2 or ends[1]-ends[0] <= tolerance:
            raise ValueError('tube end planes are unavailable')
        axial_position = (center-tube_origin).dot(tube_axis)
        if axial_position-radius <= ends[0]+tolerance or axial_position+radius >= ends[1]-tolerance:
            raise ValueError('the transverse cut must lie between the tube end planes')
        base = tube_origin + tube_axis * ends[0]
        length = ends[1]-ends[0]
        blank = cq.Solid.makeCylinder(outer, length, base, tube_axis).cut(cq.Solid.makeCylinder(bore, length, base, tube_axis))
        result = {'family': 'notched_tube', 'tube_axis': list(tube_axis.toTuple()),
                  'outer_radius_mm': outer, 'bore_radius_mm': bore}
    # Center the extended cutter on the part along its own axis, including translated parts.
    origin = center + axis * (shape.Center().dot(axis) - span)
    cutter = cq.Solid.makeCylinder(radius, 2*span, origin, axis)
    if outside:
        normals = []
        for p, normal in planes:
            if abs(normal.dot(axis)) <= 1e-9 and abs((p-center).dot(normal)) <= tolerance:
                if not any((normal-n).Length <= 1e-9 for n in normals):
                    normals.append(normal)
        if len(normals) != 1:
            raise ValueError('half-cylinder section plane is ambiguous')
        # The outward normal of the flat cut points into the removed half.
        normal = normals[0]
        plane = cq.Plane(origin=origin + axis*span, xDir=axis, normal=normal)
        half = cq.Workplane(plane).box(2*span, 2*(span+radius), span+radius, centered=(True,True,False)).val()
        cutter = cutter.intersect(half)
    difference = _same_material(shape, blank.cut(cutter))
    return {**result, 'cut_axis': list(axis.toTuple()), 'cut_origin_mm': list(center.toTuple()),
            'cut_radius_mm': radius, 'symmetric_difference_solids': difference}


def measure_machined_feature(shape, check, tolerance):
    if not shape.isValid() or len(shape.Solids()) != 1:
        raise ValueError('feature measurement requires one valid solid')
    bounds = list(_geometric_bounds(shape))
    kind, scope = check['kind'], check['scope']
    if kind == 'fillet_radius':
        geometry = _rounded_box(shape, bounds, scope, tolerance)
    elif kind.startswith('rectangular_opening_'):
        geometry = _openings(shape, bounds, scope, tolerance)
    else:
        geometry = _semicircular_cut(shape, bounds, tolerance)
    cert = {**geometry, 'bounds_mm': bounds, 'schema_version': 'machined-feature.v1',
            'complete': True, 'selection_uses_nominal': False}
    return feature_values(check, cert, tolerance), cert
