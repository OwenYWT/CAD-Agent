"""Measure complete material primitives and cavity walls from the final BRep.

Selection and construction never use requested dimensions. Every boundary of a
primitive is accounted for; a matching circular edge alone is not a cylinder.
"""
import math


def _geometry():
    try:
        import feature_verification as geometry
    except ModuleNotFoundError:
        from sandbox import feature_verification as geometry
    return geometry


def annular_segment_dimensions(shape, scope, tolerance):
    """Select an observed cylindrical band, then certify its entire material.

    The axis point chooses a face, not a sample thickness or a target-sized cut.
    Both axial endpoints and both radii come from the final BRep. The complete
    axial slice must equal that annulus, including concealed voids and bosses.
    """
    import cadquery as cq
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    g=_geometry()
    if len(shape.Solids())!=1 or not shape.isValid():
        raise ValueError('annular segment requires one valid whole solid')
    axis=g._unit(scope['axis']); point=tuple(scope['point_mm'])
    at=g._dot(point,axis)
    candidates=[]
    for index,face in enumerate(shape.Faces()):
        if face.geomType()!='CYLINDER':continue
        surface=BRepAdaptor_Surface(face.wrapped,True);cylinder=surface.Cylinder()
        if abs(abs(g._dot(g._unit(cylinder.Axis().Direction().Coord()),axis))-1)>1e-10:continue
        origin=g._axis_point(cylinder.Location().Coord(),axis)
        if g._distance(g._axis_point(point,axis),origin)>tolerance:continue
        u=(surface.FirstUParameter()+surface.LastUParameter())/2
        midpoint=surface.Value(u,(surface.FirstVParameter()+surface.LastVParameter())/2).Coord()
        radial=g._sub(g._axis_point(midpoint,axis),origin)
        if g._dot(face.normalAt(cq.Vector(*midpoint)).toTuple(),radial)<=0:continue
        lo,hi=sorted(g._dot(surface.Value(u,v).Coord(),axis) for v in
                     [surface.FirstVParameter(),surface.LastVParameter()])
        if not lo+tolerance<at<hi-tolerance:continue
        radius=cylinder.Radius();height=hi-lo
        if (face.innerWires() or abs(surface.LastUParameter()-surface.FirstUParameter()-2*math.pi)>1e-10
                or abs(face.Area()-2*math.pi*radius*height)>tolerance*(4*math.pi*radius+2*height)):
            raise ValueError('annular segment outer face is trimmed or incomplete')
        candidates.append((index,face,origin,lo,hi,radius))
    if len(candidates)!=1:
        raise ValueError('axis point does not select exactly one complete annular segment')
    index,face,origin,lo,hi,radius=candidates[0]
    holes=[hole for hole in g._cylindrical_voids(shape,tolerance)
        if hole['complete'] and abs(abs(g._dot(hole['axis'],axis))-1)<1e-10
        and g._distance(g._axis_point(hole['origin'],axis),origin)<=tolerance
        and all(s['radius'] is not None for s in hole['segments'])
        and len({round(s['radius']/tolerance) for s in hole['segments']})==1
        and min(g._dot(tuple(o+a*t for o,a in zip(hole['origin'],hole['axis'])),axis)
                for t in [hole['start'],hole['end']])<=lo+tolerance
        and max(g._dot(tuple(o+a*t for o,a in zip(hole['origin'],hole['axis'])),axis)
                for t in [hole['start'],hole['end']])>=hi-tolerance]
    if len(holes)!=1 or holes[0]['diameter']/2>=radius-tolerance:
        raise ValueError('annular segment lacks one complete coaxial constant-radius bore')
    inner=holes[0]['diameter']/2;height=hi-lo
    start=cq.Vector(*(o+a*lo for o,a in zip(origin,axis)));direction=cq.Vector(*axis)
    layer=cq.Solid.makeCylinder(radius,height,start,direction).cut(
        cq.Solid.makeCylinder(inner,height,start,direction))
    # Cover all actual material in the observed axial interval, not only the
    # expected ring. A disconnected boss/bridge cannot hide outside the ring.
    reach=shape.BoundingBox().DiagonalLength+g._distance(start.toTuple(),shape.Center().toTuple())
    actual=shape.intersect(cq.Solid.makeCylinder(reach,height,start,direction))
    missing=layer.cut(actual);excess=actual.cut(layer)
    if not all(s.isValid() for s in (layer,actual,missing,excess)) or len(layer.Solids())!=1:
        raise ValueError('annular segment material Boolean is invalid')
    volume_tolerance=layer.Area()*tolerance
    absent=sum(abs(s.Volume()) for s in missing.Solids())
    extra=sum(abs(s.Volume()) for s in excess.Solids())
    if absent>volume_tolerance or extra>volume_tolerance:
        raise ValueError('annular segment has missing or excess material')
    dimensions={'diameter':2*radius,'inner_diameter':2*inner,'height':height}
    return dimensions, {'schema_version':'annular-segment.v1','complete':True,
        'selection_uses_nominal':False,'dimensions_mm':dimensions,'axis':list(axis),
        'axis_point_mm':list(point),'origin_mm':list(origin),'axial_interval_mm':[lo,hi],
        'outer_face_index':index,'outer_surface_area_mm2':face.Area(),
        'missing_material_mm3':absent,'excess_material_mm3':extra,
        'boolean_volume_tolerance_mm3':volume_tolerance}


def primitive_dimensions(shape, primitive, tolerance):
    import cadquery as cq
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    g = _geometry()
    solids = shape.Solids()
    if len(solids) != 1 or not shape.isValid() or not solids[0].isValid():
        raise ValueError('material primitive requires exactly one valid whole solid')
    faces = solids[0].Faces()
    types = [f.geomType() for f in faces]
    if primitive == 'cylinder':
        if 'CYLINDER' not in types or any(t not in {'CYLINDER', 'PLANE'} for t in types):
            raise ValueError('complete cylindrical boundary is not certified')
        lateral = [f for f in faces if f.geomType() == 'CYLINDER']
        first = BRepAdaptor_Surface(lateral[0].wrapped, True).Cylinder()
        axis = g._unit(first.Axis().Direction().Coord())
        origin = g._axis_point(first.Location().Coord(), axis)
        radius = first.Radius()
        for face in lateral:
            surface = BRepAdaptor_Surface(face.wrapped, True)
            cylinder = surface.Cylinder()
            if (abs(cylinder.Radius()-radius) > tolerance
                    or abs(abs(g._dot(g._unit(cylinder.Axis().Direction().Coord()), axis))-1) > 1e-10
                    or g._distance(g._axis_point(cylinder.Location().Coord(), axis), origin) > tolerance):
                raise ValueError('cylindrical boundaries are not one coaxial constant-radius exterior')
            p = surface.Value((surface.FirstUParameter()+surface.LastUParameter())/2,
                              (surface.FirstVParameter()+surface.LastVParameter())/2).Coord()
            radial = g._sub(g._axis_point(p, axis), origin)
            if g._dot(face.normalAt(cq.Vector(*p)).toTuple(), radial) <= 0:
                raise ValueError('an internal bore cannot certify a material cylinder')
        caps = [f for f in faces if f.geomType() == 'PLANE']
        if len(caps) < 2 or any(abs(abs(g._dot(f.normalAt().toTuple(), axis))-1) > 1e-10 for f in caps):
            raise ValueError('cylinder caps must be perpendicular to its measured axis')
        positions = [g._dot(f.Center().toTuple(), axis) for f in caps]
        lo, hi = min(positions), max(positions)
        height = hi-lo
        if height <= tolerance or radius <= tolerance:
            raise ValueError('degenerate material cylinder')
        for face, at in zip(caps, positions):
            side = -1 if abs(at-lo) <= tolerance else 1 if abs(at-hi) <= tolerance else 0
            if not side or side*g._dot(face.normalAt().toTuple(), axis) <= 0 or face.innerWires():
                raise ValueError('caps have interior boundaries or are not the two exterior ends')
        expected_areas = [2*math.pi*radius*height, math.pi*radius**2, math.pi*radius**2]
        actual_areas = [sum(f.Area() for f in lateral),
            sum(f.Area() for f, at in zip(caps, positions) if abs(at-lo) <= tolerance),
            sum(f.Area() for f, at in zip(caps, positions) if abs(at-hi) <= tolerance)]
        if any(abs(a-b) > tolerance*(4*math.pi*radius+2*height) for a,b in zip(actual_areas, expected_areas)):
            raise ValueError('the whole closed cylinder boundary is incomplete')
        dimensions = {'diameter':2*radius, 'radius':radius, 'height':height}
        geometry = {'axis':list(axis), 'origin_mm':list(origin), 'axial_interval_mm':[lo,hi]}
    elif primitive == 'cube':
        # Certify the whole six-sided boundary, not a bounding box or a
        # nominally sized sample. Extra faces, inner wires and curved edges
        # retain even tiny pockets/cuts as failures of this certificate.
        if (len(faces) != 6 or any(t != 'PLANE' for t in types)
                or len(shape.Faces()) != 6 or len(shape.Edges()) != 12 or len(shape.Vertices()) != 8
                or any(f.innerWires() or len(f.Edges()) != 4
                       or any(e.geomType() != 'LINE' for e in f.Edges()) for f in faces)):
            raise ValueError('a cube requires six complete planar quadrilateral exterior faces')
        axes, pairs = [], []
        for i, face in enumerate(faces):
            normal = g._unit(face.normalAt().toTuple())
            parallel = [j for j, axis in enumerate(axes) if abs(abs(g._dot(axis, normal))-1) <= 1e-10]
            if parallel:
                pairs[parallel[0]].append(i)
            else:
                if any(abs(g._dot(axis, normal)) > 1e-10 for axis in axes):
                    raise ValueError('cube faces are not mutually perpendicular')
                axes.append(normal)
                pairs.append([i])
        if len(pairs) != 3 or any(len(pair) != 2 for pair in pairs):
            raise ValueError('cube faces are not three opposed exterior pairs')
        intervals = []
        for axis, pair in zip(axes, pairs):
            positions = [g._dot(faces[i].Center().toTuple(), axis) for i in pair]
            lo, hi = min(positions), max(positions)
            if hi-lo <= tolerance or any((p-(lo+hi)/2)*g._dot(faces[i].normalAt().toTuple(), axis) <= 0
                                         for i, p in zip(pair, positions)):
                raise ValueError('cube face pairs do not enclose positive material')
            intervals.append([lo, hi])
        lengths = [hi-lo for lo, hi in intervals]
        if max(lengths)-min(lengths) > tolerance:
            raise ValueError('the three independently measured cube edges are unequal')
        dimensions = {'edge': sum(lengths)/3}
        geometry = {'axes': [list(axis) for axis in axes], 'intervals_mm': intervals,
                    'face_axis_pairs': pairs, 'edge_count': 12, 'vertex_count': 8,
                    'inner_boundary_counts': [len(f.innerWires()) for f in faces],
                    'boundary_edge_counts': [len(f.Edges()) for f in faces]}
    elif primitive == 'sphere':
        if not faces or any(t != 'SPHERE' for t in types):
            raise ValueError('complete spherical boundary is not certified')
        first = BRepAdaptor_Surface(faces[0].wrapped, True).Sphere()
        radius, center = first.Radius(), first.Location().Coord()
        for face in faces:
            surface = BRepAdaptor_Surface(face.wrapped, True)
            sphere = surface.Sphere()
            p = surface.Value((surface.FirstUParameter()+surface.LastUParameter())/2,
                              (surface.FirstVParameter()+surface.LastVParameter())/2).Coord()
            if (abs(sphere.Radius()-radius) > tolerance or g._distance(sphere.Location().Coord(), center) > tolerance
                    or g._dot(face.normalAt(cq.Vector(*p)).toTuple(), g._sub(p, center)) <= 0):
                raise ValueError('sphere boundaries do not describe one material exterior')
        if abs(sum(f.Area() for f in faces)-4*math.pi*radius**2) > 8*math.pi*radius*tolerance:
            raise ValueError('the whole closed sphere boundary is incomplete')
        dimensions, geometry = {'diameter':2*radius,'radius':radius}, {'center_mm':list(center)}
    else:
        raise ValueError('unsupported material primitive')
    return dimensions, {'schema_version':'material-primitive.v1', 'primitive':primitive,
        'dimensions_mm':dimensions, 'geometry':geometry, 'solid_count':1,
        'face_count':len(faces), 'face_types':types, 'covered_face_indices':list(range(len(faces))),
        'complete':True, 'selection_uses_nominal':False}


def cavity_wall_thickness(shape, mode, tolerance):
    """Find a unique open rectangular cavity by concave floor/wall adjacency.

    The cavity's actual floor identifies all four side walls, including rotated
    boxes. A complete opposing boundary and solid material layer certify each
    thickness. Extra holes cannot be hidden by a volume-error allowance.
    """
    import cadquery as cq

    g = _geometry()
    if len(shape.Solids()) != 1 or not shape.isValid():
        raise ValueError('cavity wall selection requires one valid whole solid')
    faces = shape.Faces()
    candidates = []
    for floor_index, floor in enumerate(faces):
        if floor.geomType() != 'PLANE':
            continue
        edges = floor.outerWire().Edges()
        if len(edges) != 4 or any(e.geomType() != 'LINE' for e in edges):
            continue
        normal = floor.normalAt()
        sides = []
        for edge in edges:
            adjacent = [(i,f) for i,f in enumerate(faces) if i != floor_index
                        and any(e.wrapped.IsSame(edge.wrapped) for e in f.Edges())]
            if len(adjacent) != 1:
                break
            i, wall = adjacent[0]
            if (wall.geomType() != 'PLANE' or abs(wall.normalAt().dot(normal)) > 1e-10
                    or (wall.Center()-floor.Center()).dot(normal) <= tolerance
                    or (floor.Center()-wall.Center()).dot(wall.normalAt()) <= tolerance):
                break
            sides.append((i,wall,edge))
        if len(sides) != 4 or len({i for i,_,_ in sides}) != 4:
            continue
        if any(abs(sides[i][1].normalAt().dot(sides[(i+1)%4][1].normalAt())) > 1e-10 for i in range(4)):
            continue
        height = max((v.Center()-floor.Center()).dot(normal) for v in shape.Vertices())
        void = cq.Solid.extrudeLinear(floor.outerWire(), [], normal*(height + 10*tolerance))
        intrusion = g._measurement_common(void, shape, 'cavity_opening')
        if intrusion is not None and any(s.Volume() > 0 for s in intrusion.Solids()):
            continue
        candidates.append((floor_index, floor, sides, height))
    if len(candidates) != 1:
        raise ValueError('a unique complete open rectangular cavity could not be identified')
    floor_index, floor, sides, height = candidates[0]
    selected = sides if mode == 'side_walls' else [(floor_index,floor,None)]
    values, certificates = [], []
    for index, face, edge in selected:
        if face.innerWires():
            raise ValueError('cavity wall has an unaccounted inner boundary')
        if edge is not None:
            # The floor-adjacent patch alone does not cover a stepped wall or
            # an open notch. Certify its complete rectangular domain up to the
            # measured opening, independently of the requested thickness.
            p, q = edge.startPoint(), edge.endPoint()
            points = [p,q,q+floor.normalAt()*height,p+floor.normalAt()*height]
            rectangle = cq.Face.makeFromWires(cq.Wire.makePolygon(points,close=True))
            for before, after in ((rectangle,face),(face,rectangle)):
                remainder = g._measurement_boolean(before, after, 'cavity_face_coverage', difference=True)
                if remainder is not None and any(f.Area() > 0 for f in remainder.Faces()):
                    raise ValueError('the selected patch does not cover the complete cavity wall')
        row = {'measured':[], 'details':{}}
        layer = g._planar_normal_wall(shape, face, face.Center(), tolerance, row)
        if layer is None:
            raise ValueError('the entire cavity wall has no complete opposed material layer')
        missing = g._measurement_boolean(layer, shape, 'cavity_wall_material', difference=True)
        if missing is not None and any(s.Volume() > 0 for s in missing.Solids()):
            raise ValueError('the wall contains unaccounted missing material')
        values.extend(row['measured'])
        certificates.append({'face_index':index, 'thickness_mm':row['measured'][0],
            'certified_area_mm2':face.Area(), 'material_complete':True,
            'inner_boundary_count':0, 'complete_face':True})
    return values, {'schema_version':'cavity-walls.v1', 'mode':mode, 'floor_face_index':floor_index,
        'side_face_indices':[i for i,_,_ in sides], 'layers':certificates, 'cavity_height_mm':height,
        'complete':True, 'selection_uses_nominal':False}
