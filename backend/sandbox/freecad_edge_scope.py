"""Conservative edge-scope resolution from actual BRep topology, never labels.

Outer scope is supported for solids whose exterior faces are supporting planes,
with optional internal cylindrical bores. Other surfaces/concave exterior
boundaries require an explicit topology selection instead of a guessed scope.
"""
import math


class EdgeScopeError(ValueError):
    pass


def _contains(wire, edge):
    return any(item.isSame(edge) for item in wire.Edges)


def _face_kind(face, vertices, tolerance):
    u0, u1, v0, v1 = face.ParameterRange
    u, v = (u0+u1)/2, (v0+v1)/2
    point, normal = face.valueAt(u,v), face.normalAt(u,v)
    surface = face.Surface
    if surface.TypeId == 'Part::GeomPlane':
        if all((vertex.Point-point).dot(normal) <= tolerance for vertex in vertices):
            return 'exterior_plane'
        raise EdgeScopeError('non-supporting planar boundary requires explicit edge selection')
    if surface.TypeId == 'Part::GeomCylinder':
        axis = surface.Axis
        relative = point-surface.Center
        radial = relative-axis.multiply(relative.dot(axis))
        if radial.Length > tolerance and normal.dot(radial) < -tolerance:
            return 'bore'
    raise EdgeScopeError('unsupported or ambiguous curved boundary; select an explicit edge')


def resolve_edge_scope(shape, scope):
    if scope not in {'outer','hole_mouths','all'}:
        raise EdgeScopeError('unknown edge scope')
    if shape.isNull() or not shape.isValid() or len(shape.Solids) != 1:
        raise EdgeScopeError('edge scope requires one valid solid')
    if scope == 'all':
        return [f'Edge{i+1}' for i in range(len(shape.Edges))], []
    tolerance = max(1e-7, shape.BoundBox.DiagonalLength * 1e-9)
    kinds = [_face_kind(face,shape.Vertexes,tolerance) for face in shape.Faces]
    selected = []
    for index, edge in enumerate(shape.Edges):
        adjacent = [(face,kind) for face,kind in zip(shape.Faces,kinds)
                    if any(item.isSame(edge) for item in face.Edges)]
        exterior = [(face,kind) for face,kind in adjacent if kind == 'exterior_plane']
        bores = [(face,kind) for face,kind in adjacent if kind == 'bore']
        if len(exterior) == 2 and all(_contains(face.OuterWire,edge) for face,_ in exterior):
            category = 'outer'
        elif (len(exterior) == 1 and len(bores) == 1
              and not _contains(exterior[0][0].OuterWire,edge)
              and edge.Curve.TypeId == 'Part::GeomCircle'):
            category = 'hole_mouths'
        elif len(bores) == 1 and not exterior:
            # Periodic cylinder seam is not an exterior edge or a hole mouth.
            continue
        else:
            raise EdgeScopeError('edge adjacency is ambiguous; select explicit topology')
        if category == scope:
            selected.append(f'Edge{index+1}')
    if not selected:
        raise EdgeScopeError(f'no edges satisfy {scope}')
    protected = [face.copy() for face,kind in zip(shape.Faces,kinds) if kind == 'bore'] if scope == 'outer' else []
    return selected, protected


def verify_protected_faces(before_faces, after_shape):
    for before in before_faces:
        remainder = before
        for after in after_shape.Faces:
            if after.Surface.TypeId == before.Surface.TypeId:
                remainder = remainder.cut(after)
        if not math.isfinite(remainder.Area) or remainder.Area > max(1e-7,before.Area*1e-8):
            raise EdgeScopeError('edge treatment changed a protected bore surface')
