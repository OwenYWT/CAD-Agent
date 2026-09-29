"""Independent measurements of exported BRep, never generated-plan assertions.

Unsupported or ambiguous measurements return indeterminate. Units are mm, mm³,
or an exact count. Numerical tolerance comes from OCCT, not a percentage of the
requested dimension. No benchmark IDs or modeling recipes belong in this file.
"""
from __future__ import annotations

import math


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _sub(a, b):
    return tuple(x-y for x, y in zip(a, b))


def _unit(axis):
    length = math.hypot(*axis)
    if not length:
        raise ValueError("measurement axis is zero")
    return tuple(x/length for x in axis)


def _axis_point(point, axis):
    return _sub(point, tuple(_dot(point, axis)*v for v in axis))


def _distance(a, b):
    return math.hypot(*_sub(a, b))


def _surface_clearance(shape, scope, tolerance):
    """OCCT extremum of full trimmed faces, rather than selected-point distance.

Selection probes identify surfaces; they are not samples used to certify an
unsampled region. This is minimum Euclidean clearance, not normal wall thickness.
"""
    import cadquery as cq
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    probes = scope.get('centers_mm', [])
    if len(probes) != 2:
        raise ValueError('surface clearance requires two explicit surface points')
    all_faces = shape.Faces()
    faces, indices = [], []
    for probe in probes:
        vertex = cq.Vertex.makeVertex(*probe)
        matches = [(i, f) for i, f in enumerate(all_faces) if f.distance(vertex) <= tolerance]
        if len(matches) != 1:
            raise ValueError('surface point does not identify exactly one trimmed face')
        index, face = matches[0]
        # Boundary/seam probes do not unambiguously express a surface interior.
        if any(edge.distance(vertex) <= tolerance for edge in face.Edges()):
            raise ValueError('surface selection point lies on a boundary or seam')
        indices.append(index); faces.append(face)
    if indices[0] == indices[1]:
        raise ValueError('surface clearance requires distinct faces')
    extrema = BRepExtrema_DistShapeShape(faces[0].wrapped, faces[1].wrapped)
    extrema.SetDeflection(tolerance)
    extrema.Perform()
    if not extrema.IsDone() or extrema.NbSolution() < 1:
        raise ValueError('OCCT surface distance did not converge')
    distance = float(extrema.Value())
    if not math.isfinite(distance) or distance < 0:
        raise ValueError('OCCT returned an invalid surface distance')
    return distance, {
        'scope': 'minimum Euclidean distance over the two complete trimmed faces',
        'normal_wall_thickness': 'unverified', 'other_faces': 'unverified',
        'face_indices': indices, 'face_geometry': [f.geomType() for f in faces],
        'face_area_mm2': [f.Area() for f in faces],
        'closest_points_mm': [list(extrema.PointOnShape1(1).Coord()), list(extrema.PointOnShape2(1).Coord())],
        'extremum_solutions': extrema.NbSolution(),
    }


def _closed_offset_wall(shape, face, tolerance):
    """Exact normal-offset proof for complete spherical/toroidal boundaries.

Analytic identity proves constant normal distance; an independent BRep Boolean
proves the *whole* swept layer contains material. It also detects hidden cavities.
Freeform surfaces without a proven offset correspondence remain unverified.
"""
    import cadquery as cq
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    def signature(item):
        kind = item.geomType()
        if kind not in {'SPHERE', 'TORUS'}:
            return None
        s = BRepAdaptor_Surface(item.wrapped, True)
        native = s.Sphere() if kind == 'SPHERE' else s.Torus()
        center = native.Location().Coord()
        minor = native.Radius() if kind == 'SPHERE' else native.MinorRadius()
        major = 0 if kind == 'SPHERE' else native.MajorRadius()
        if minor <= tolerance or (kind == 'TORUS' and major <= minor+tolerance):
            return None
        area = 4*math.pi*minor**2 if kind == 'SPHERE' else 4*math.pi**2*major*minor
        if abs(item.Area()-area) > area*tolerance/minor:
            return None  # Trimmed or incomplete boundary.
        axis = _unit(native.Position().Direction().Coord())
        u = (s.FirstUParameter()+s.LastUParameter())/2
        v = (s.FirstVParameter()+s.LastVParameter())/2
        p = s.Value(u, v).Coord()
        radial = _sub(p, center)
        if major:
            planar = _sub(radial, tuple(_dot(radial,axis)*a for a in axis))
            radial = _sub(radial, tuple(major*a for a in _unit(planar)))
        orientation = _dot(item.normalAt(cq.Vector(*p)).toTuple(), radial)
        return {'kind': kind, 'center': center, 'axis': axis, 'minor': minor,
                'major': major, 'orientation': orientation}

    selected = signature(face)
    if selected is None:
        raise ValueError('selected curved surface has no certified normal-offset correspondence')
    candidates = []
    for other in shape.Faces():
        match = signature(other)
        if (match is None or match['kind'] != selected['kind']
                or _distance(match['center'], selected['center']) > tolerance
                or abs(match['major']-selected['major']) > tolerance
                or match['orientation']*selected['orientation'] >= 0
                or (selected['major'] and abs(abs(_dot(match['axis'],selected['axis']))-1) > tolerance)):
            continue
        outer, inner = sorted([match,selected], key=lambda s:s['minor'], reverse=True)
        if outer['orientation'] <= 0 or inner['orientation'] >= 0:
            continue
        def primitive(s):
            if s['kind'] == 'SPHERE':
                return cq.Solid.makeSphere(s['minor'], s['center'], angleDegrees1=-90, angleDegrees2=90)
            return cq.Solid.makeTorus(s['major'], s['minor'], s['center'], s['axis'])
        layer = primitive(outer).cut(primitive(inner))
        if not layer.isValid() or len(layer.Solids()) != 1:
            continue
        missing = layer.cut(shape).Volume()
        error = layer.Area()*tolerance
        if missing <= error:
            candidates.append((outer['minor']-inner['minor'], missing, error, layer))
    if len(candidates) != 1:
        raise ValueError('normal-offset boundary or complete material layer is missing or ambiguous')
    depth, missing, error, layer = candidates[0]
    return depth, {'scope':'entire selected closed analytic surface',
        'certified_area_mm2': face.Area(), 'surface_geometry': selected['kind'],
        'missing_material_mm3': missing, 'boolean_volume_tolerance_mm3': error}, layer


def _general_normal_wall(shape, face, point, tolerance):
    """Certify a complete curved face using its actual normal offset and material.

The depth comes from an independent material intersection at the explicit
selection point, never from the requested nominal. A local witness alone cannot
pass: the entire offset boundary and swept material layer must also be proven.
Unsupported offsets, folds and incomplete boundary correspondence fail closed.
"""
    import cadquery as cq
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffsetShape, BRepOffsetAPI_MakeThickSolid
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Check

    normal = face.normalAt(point).normalized()
    reach = shape.BoundingBox().DiagonalLength + (point-shape.Center()).Length
    line = cq.Edge.makeLine(point-normal*reach, point+normal*reach)
    intervals = []
    for edge in shape.intersect(line).Edges():
        ends = sorted([(edge.startPoint()-point).dot(normal), (edge.endPoint()-point).dot(normal)])
        if min(abs(x) for x in ends) <= tolerance and ends[1]-ends[0] > tolerance:
            intervals.append(ends)
    if len(intervals) != 1:
        raise ValueError('normal ray has no unique boundary-adjacent material interval')
    depth = intervals[0][1]-intervals[0][0]
    other_faces = [f for f in shape.Faces() if not f.wrapped.IsSame(face.wrapped)]
    certificates = []
    diagnostics = []
    for sign in (-1, 1):
        try:
            builder = BRepOffsetAPI_MakeOffsetShape()
            builder.PerformBySimple(face.wrapped, sign*depth)
            thickener = BRepOffsetAPI_MakeThickSolid()
            thickener.MakeThickSolidBySimple(face.wrapped, sign*depth)
            if not builder.IsDone() or not thickener.IsDone():
                diagnostics.append('offset_construction_not_done'); continue
            boundary = cq.Shape.cast(builder.Shape())
            layer = cq.Shape.cast(thickener.Shape())
            if (len(layer.Solids()) != 1 or layer.Volume() <= 0 or not layer.isValid()
                    or not BRepAlgoAPI_Check(layer.wrapped).IsValid()):
                diagnostics.append('offset_layer_invalid_or_self_intersecting'); continue
            area_error = sum(e.Length() for e in boundary.Edges())*tolerance + tolerance**2
            uncovered = boundary.cut(*other_faces, tol=tolerance).Area()
            missing = layer.cut(shape, tol=tolerance).Volume()
            volume_error = layer.Area()*tolerance
            if uncovered > area_error or missing > volume_error:
                diagnostics.append('offset_boundary_or_entire_material_layer_not_matched'); continue
            certificates.append(({'scope':'entire selected trimmed curved face',
                'surface_geometry':face.geomType(), 'certified_area_mm2':face.Area(),
                'normal_witness_point_mm':list(point.toTuple()),
                'normal_witness_interval_mm':intervals[0],
                'offset_boundary_area_mm2':boundary.Area(), 'uncovered_boundary_area_mm2':uncovered,
                'boundary_area_tolerance_mm2':area_error, 'missing_material_mm3':missing,
                'boolean_volume_tolerance_mm3':volume_error, 'self_intersections_checked':True}, layer))
        except Exception as exc:
            diagnostics.append(f'{type(exc).__name__}:{exc}')
    if len(certificates) != 1:
        raise ValueError('whole curved-face normal wall not certified: '+ '; '.join(diagnostics))
    return depth, *certificates[0]


def _planar_normal_wall(shape, face, point, tolerance, row):
    import cadquery as cq
    normal = face.normalAt(point)
    opposite=[f for f in shape.Faces() if f.geomType()=='PLANE'
        and f.normalAt().dot(normal)<-1+tolerance]
    distances=[]
    for f in opposite:
        depth=-(f.Center()-point).dot(normal)
        if depth>tolerance and all(abs(depth-d)>tolerance for d in distances):distances.append(depth)
    layer = None
    for depth in sorted(distances):
        translated=face.translate((-normal*depth).toTuple())
        coverage=[f for f in opposite if abs((f.Center()-point).dot(normal)+depth)<=tolerance]
        # The whole opposing surface must exist, not just one
        # sampled ray. The swept prism must contain no void.
        if translated.cut(*coverage).Area()>sum(e.Length() for e in face.Edges())*tolerance:continue
        prism=cq.Solid.extrudeLinear(face.outerWire(),face.innerWires(),-normal*depth)
        if not prism.isValid() or len(prism.Solids())!=1 or prism.Volume()<=0:
            continue
        if prism.cut(shape).Volume()<=prism.Area()*tolerance:
            row['measured']=[depth];layer=prism;break
    row['method']='whole_planar_face_opposed_boundary_and_material_prism'
    row['details']['scope']='entire selected planar face; not every wall of the part'
    row['details']['certified_area_mm2']=face.Area() if row['measured'] else 0
    return layer


def _continuous_normal_wall(shape, tolerance):
    """Cover all material with independently certified constant-normal layers.

    A ray only proposes a measured depth. Complete opposing boundary, material,
    validity and coverage tests are required. The requested nominal is never an
    input to construction or selection. Overlapping competing layers which add
    new material remain ambiguous, rather than concealing a thick junction.
    """
    import cadquery as cq
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    candidates = []
    rejected = []
    for solid_index, solid in enumerate(shape.Solids()):
        for face_index, face in enumerate(solid.Faces()):
            try:
                analytic = None
                if face.geomType() in {'SPHERE', 'TORUS'}:
                    try:
                        analytic = _closed_offset_wall(solid, face, tolerance)
                    except ValueError:
                        pass  # A trimmed analytic patch can still have a general offset proof.
                if analytic:
                    depth, details, layer = analytic
                else:
                    surface=BRepAdaptor_Surface(face.wrapped,True)
                    u=(surface.FirstUParameter()+surface.LastUParameter())/2
                    v=(surface.FirstVParameter()+surface.LastVParameter())/2
                    point=cq.Vector(*surface.Value(u,v).Coord())
                    vertex=cq.Vertex.makeVertex(*point.toTuple())
                    if face.distance(vertex)>tolerance or any(e.distance(vertex)<=tolerance for e in face.Edges()):
                        raise ValueError('no unambiguous interior witness for this face')
                    if face.geomType()=='PLANE':
                        row={'measured':[], 'details':{}}
                        layer=_planar_normal_wall(solid,face,point,tolerance,row)
                        if layer is None:
                            raise ValueError('complete planar normal layer not certified')
                        depth, details=row['measured'][0],row['details']
                    else:
                        depth, details, layer=_general_normal_wall(solid,face,point,tolerance)
                if not math.isfinite(depth) or depth<=tolerance or not layer.isValid() or layer.Volume()<=0:
                    raise ValueError('normal layer is invalid')
                candidates.append((depth, solid_index, face_index, layer, details))
            except Exception as exc:
                rejected.append({'solid_index':solid_index,'face_index':face_index,'reason':str(exc)})
    covered=None
    selected=[]
    for depth, solid_index, face_index, layer, details in sorted(candidates,key=lambda c:c[:3]):
        uncertainty=layer.Area()*tolerance
        if covered is not None:
            if layer.cut(covered).Volume()<=uncertainty:
                continue
            if layer.intersect(covered).Volume()>uncertainty:
                continue  # Cannot certify a junction by superposing two walls.
        covered=layer if covered is None else covered.fuse(layer)
        if not covered.isValid():
            raise ValueError('normal-layer union is invalid')
        selected.append({'thickness_mm':depth,'solid_index':solid_index,'face_index':face_index,
                         'certificate':details})
    if covered is None:
        raise ValueError('no complete normal material layers could be certified')
    missing=shape.cut(covered).Volume()
    extra=covered.cut(shape).Volume()
    error=(shape.Area()+covered.Area())*tolerance
    if missing>error or extra>error:
        raise ValueError(f'normal layers do not cover the whole part: uncovered={missing}, extra={extra}, numerical_volume_tolerance={error}')
    return [item['thickness_mm'] for item in selected], {
        'scope':'entire part material covered by nonoverlapping certified normal-offset layers',
        'uncovered_material_mm3':missing,'extra_material_mm3':extra,
        'boolean_volume_tolerance_mm3':error,'layers':selected,
        'unusable_face_certificates':rejected,
        'inference':'interior witnesses determine candidate depths; whole-boundary and whole-material proofs determine coverage',
    }


def _cylindrical_voids(shape, tolerance):
    """Group concave complete cylindrical segments into coaxial passages.

    A bore split into faces at a seam or counterbore remains one passage.
    Incomplete angular surfaces are not silently treated as round holes.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    groups = []
    for face in shape.Faces():
        if face.geomType() not in {"CYLINDER", "CONE"}:
            continue
        surface = BRepAdaptor_Surface(face.wrapped, True)
        is_cylinder = face.geomType() == "CYLINDER"
        cylinder = surface.Cylinder() if is_cylinder else surface.Cone()
        axis = _unit(cylinder.Axis().Direction().Coord())
        if next(v for v in axis if abs(v) > tolerance) < 0:
            axis = tuple(-v for v in axis)
        location = cylinder.Location().Coord()
        origin = _axis_point(location, axis)
        u = (surface.FirstUParameter()+surface.LastUParameter())/2
        v = (surface.FirstVParameter()+surface.LastVParameter())/2
        point = surface.Value(u, v).Coord()
        radial = _sub(_axis_point(point, axis), origin)
        normal = face.normalAt(__import__('cadquery').Vector(*point)).toTuple()
        if _dot(normal, radial) >= 0:
            continue  # An outside boss is not a hole.
        ends = [surface.Value(u, t).Coord() for t in (surface.FirstVParameter(), surface.LastVParameter())]
        interval = sorted(_dot(p, axis) for p in ends)
        segment = {"radius": float(cylinder.Radius()) if is_cylinder else None, "interval": interval,
                   "largest_radius":max(_distance(_axis_point(p,axis),origin) for p in ends),
                   "angular_span": abs(surface.LastUParameter()-surface.FirstUParameter())}
        group = next((g for g in groups if _distance(g['axis'], axis) < tolerance
                      and _distance(g['origin'], origin) < tolerance), None)
        if group is None:
            group = {'axis': axis, 'origin': origin, 'segments': []}
            groups.append(group)
        group['segments'].append(segment)
    passages = []
    for group in groups:
        ordered = sorted(group['segments'], key=lambda s:s['interval'][0])
        components = []
        for segment in ordered:
            if not components or segment['interval'][0] > components[-1]['end']+tolerance:
                components.append({'segments':[], 'start':segment['interval'][0], 'end':segment['interval'][1]})
            component = components[-1]
            component['segments'].append(segment)
            component['end'] = max(component['end'], segment['interval'][1])
        for component in components:
            # Require complete circles on every axial/radius band. Tangential
            # openings or arbitrary side cuts need a more general void detector.
            bands = []
            for s in component['segments']:
                band = next((b for b in bands if b['radius'] is not None and s['radius'] is not None
                             and abs(b['radius']-s['radius']) <= tolerance
                             and max(abs(a-bb) for a,bb in zip(b['interval'],s['interval'])) <= tolerance),None)
                if band is None:
                    bands.append(dict(s))
                else:
                    band['angular_span'] += s['angular_span']
            complete = all(abs(s['angular_span']-2*math.pi) <= tolerance for s in bands)
            cylinders = [s for s in bands if s['radius'] is not None]
            if not cylinders:
                continue  # Pure conical voids are outside the round-bore contract.
            shaft_radius=min(s['radius'] for s in cylinders)
            passages.append({**group, **component, 'complete':complete,
                             'diameter':2*shaft_radius,
                             'largest_section_diameter':2*max(s['largest_radius'] for s in bands),
                             'shaft_length':sum(s['interval'][1]-s['interval'][0] for s in cylinders if abs(s['radius']-shaft_radius)<=tolerance),
                             'depth':component['end']-component['start']})
    return passages


def _scope_passages(passages, scope, tolerance):
    axis = _unit(scope['axis']) if scope.get('axis') else None
    result = []
    for p in passages:
        if axis and abs(abs(_dot(axis,p['axis']))-1) > tolerance:
            continue
        if scope.get('point_mm') is not None:
            if _distance(_axis_point(scope['point_mm'],p['axis']),p['origin']) > tolerance:
                continue
        if scope.get('region_min_mm') is not None:
            midpoint = tuple(o+a*(p['start']+p['end'])/2 for o,a in zip(p['origin'],p['axis']))
            if not all(lo-tolerance <= v <= hi+tolerance for lo,v,hi in
                       zip(scope['region_min_mm'],midpoint,scope['region_max_mm'])):
                continue
        result.append(p)
    return result


def measure_checks(shape, checks):
    import cadquery as cq
    from OCP.Precision import Precision

    tolerance = float(Precision.Confusion_s())
    evidence = []
    passages = None
    for check in checks:
        row = {'check_id':check['check_id'], 'outcome':'indeterminate',
               'method':'final_step_brep', 'measured':[], 'issues':[],
               'details':{'numerical_tolerance_mm':tolerance}}
        kind = check['kind']; scope = check.get('scope') or {}
        try:
            frame=scope.get('frame','world')
            if frame!='world':
                box=shape.BoundingBox()
                origin=((box.xmin,box.ymin,box.zmin) if frame=='bounds_min'
                        else ((box.xmin+box.xmax)/2,(box.ymin+box.ymax)/2,(box.zmin+box.zmax)/2))
                scope=dict(scope)
                for field in ['point_mm','region_min_mm','region_max_mm']:
                    if scope.get(field) is not None:scope[field]=[v+o for v,o in zip(scope[field],origin)]
                scope['centers_mm']=[[v+o for v,o in zip(point,origin)] for point in scope.get('centers_mm',[])]
                row['details']['resolved_frame_origin_mm']=origin
            if kind == 'surface_clearance':
                distance, details = _surface_clearance(shape, scope, tolerance)
                row['measured'] = [distance]
                row['method'] = 'occt_trimmed_face_global_minimum_distance'
                row['details'].update(details)
            elif kind == 'solid_count':
                row['method'] = 'brep_solid_count'
                row['measured'] = [len(shape.Solids())]
            elif kind == 'void_connected':
                lo=scope['region_min_mm'];hi=scope['region_max_mm']
                box=cq.Solid.makeBox(*(b-a for a,b in zip(lo,hi)),pnt=cq.Vector(*lo))
                void=box.cut(shape);components=void.Solids();membership=[]
                for xyz in scope['centers_mm']:
                    point=cq.Vector(*xyz)
                    if not all(a+tolerance<v<b-tolerance for a,v,b in zip(lo,xyz,hi)):
                        raise ValueError('connectivity probe must be inside its region')
                    vertex=cq.Vertex.makeVertex(*xyz)
                    matches=[i for i,s in enumerate(components) if s.isInside(point,tolerance)
                        and all(f.distance(vertex)>tolerance for f in s.Faces())]
                    if len(matches)!=1:
                        raise ValueError('connectivity probe is not uniquely inside a void')
                    membership.append(matches[0])
                row['method']='connected_brep_void_components_in_explicit_region'
                row['details']['region_mm']=[lo,hi]
                row['details']['probe_components']=membership
                row['measured']=[int(len(set(membership))==1)]
            elif kind == 'volume':
                row['method'] = 'brep_material_volume'
                row['measured'] = [sum(s.Volume() for s in shape.Solids())]
                # OCCT linear precision propagated through the measured surface
                # area gives a first-order volume uncertainty, in mm³.
                row['details']['numerical_tolerance_mm3'] = shape.Area()*tolerance
            elif kind == 'overall_dimension':
                axis = _unit(scope['axis'])
                # Rotate the explicit measurement axis to Z before measuring;
                # orientation must not be inferred by sorting three dimensions.
                z = cq.Vector(0,0,1); direction = cq.Vector(*axis)
                cross = direction.cross(z)
                transformed = shape
                if cross.Length > tolerance:
                    angle = math.degrees(math.acos(max(-1,min(1,_dot(axis,(0,0,1))))))
                    transformed = shape.rotate((0,0,0),cross.toTuple(),angle)
                row['method'] = 'brep_extent_on_explicit_axis'
                row['measured'] = [transformed.BoundingBox().zlen]
            elif kind.startswith('hole_'):
                if passages is None:
                    passages = _cylindrical_voids(shape,tolerance)
                holes = _scope_passages(passages,scope,tolerance)
                row['method'] = 'concave_cylindrical_passages'
                row['details']['scope'] = 'complete cylindrical passages only'
                if any(not h['complete'] for h in holes):
                    row['issues'].append('incomplete_cylindrical_boundary')
                elif kind == 'hole_count':
                    row['measured'] = [len(holes)]
                elif kind in {'hole_diameter','hole_depth'}:
                    if not holes:
                        row['outcome'] = 'failed';row['issues'].append('no_matching_cylindrical_passage')
                    else:
                        quantity = (('largest_section_diameter' if scope.get('hole_diameter_mode')=='largest_section' else 'diameter') if kind=='hole_diameter' else
                                    'shaft_length' if scope.get('hole_depth_mode')=='shaft_length' else 'depth')
                        row['measured'] = [h[quantity] for h in holes]
                        row['details']['quantity'] = quantity
                elif kind == 'hole_position':
                    axis = _unit(scope['axis']);remaining = list(holes);distances=[]
                    for expected in scope['centers_mm']:
                        if not remaining:
                            break
                        center = _axis_point(expected,axis)
                        nearest = min(remaining,key=lambda h:_distance(h['origin'],center))
                        distances.append(_distance(nearest['origin'],center));remaining.remove(nearest)
                    row['method'] = 'hole_axes_transverse_locations'
                    if remaining or len(distances)!=len(scope['centers_mm']):
                        row['outcome']='failed';row['issues'].append('hole_position_count_mismatch')
                    else:
                        row['measured']=distances
            elif kind == 'wall_thickness':
                mode = scope.get('wall_mode','continuous_normal')
                if mode == 'surface_normal' and scope.get('point_mm') is not None:
                    point=cq.Vector(*scope['point_mm']);vertex=cq.Vertex.makeVertex(*scope['point_mm'])
                    faces=[f for f in shape.Faces() if f.distance(vertex)<=tolerance]
                    if len(faces) == 1 and faces[0].geomType() != 'PLANE':
                        owners = [s for s in shape.Solids()
                                  if any(f.wrapped.IsSame(faces[0].wrapped) for f in s.Faces())]
                        if len(owners) != 1:
                            raise ValueError('selected face has no unique solid owner')
                        try:
                            depth, details, _ = _closed_offset_wall(owners[0], faces[0], tolerance)
                            row['method'] = 'analytic_normal_offset_and_whole_material_layer'
                        except ValueError:
                            depth, details, _ = _general_normal_wall(owners[0], faces[0], point, tolerance)
                            row['method'] = 'whole_curved_face_offset_boundary_and_material_layer'
                        row['measured'] = [depth]
                        row['details'].update(details)
                    elif len(faces)!=1:
                        raise ValueError('surface-normal certificate requires a point inside one planar face')
                    else:
                        _planar_normal_wall(shape, faces[0], point, tolerance, row)
                elif mode == 'continuous_normal':
                    if any(scope.get(key) is not None for key in ('region_min_mm','region_max_mm','point_mm')) or scope.get('centers_mm'):
                        raise ValueError('whole-part normal thickness cannot silently apply a restricted measurement scope')
                    row['measured'], details = _continuous_normal_wall(shape,tolerance)
                    row['method']='whole_part_certified_normal_layer_coverage'
                    row['details'].update(details)
                elif mode == 'local_probe' and scope.get('point_mm') is not None and scope.get('axis'):
                    point=cq.Vector(*scope['point_mm']);axis=cq.Vector(*_unit(scope['axis']))
                    if not shape.isInside(point,tolerance):
                        row['issues'].append('wall_probe_is_not_inside_material')
                    else:
                        reach=shape.BoundingBox().DiagonalLength+_distance(point.toTuple(),shape.Center().toTuple())
                        line=cq.Edge.makeLine(point-axis*reach,point+axis*reach)
                        intervals=shape.intersect(line).Edges()
                        for edge in intervals:
                            a=(edge.startPoint()-point).dot(axis);b=(edge.endPoint()-point).dot(axis)
                            if min(a,b)-tolerance<=0<=max(a,b)+tolerance:
                                row['measured'].append(abs(b-a))
                        row['method']='material_interval_at_explicit_probe'
                        row['details']['scope']='local probe only; not a whole-surface certificate'
                elif mode == 'radial':
                    from OCP.BRepAdaptor import BRepAdaptor_Surface
                    if passages is None: passages=_cylindrical_voids(shape,tolerance)
                    holes=_scope_passages(passages,scope,tolerance)
                    for hole in holes:
                        candidates=[]
                        for face in shape.Faces():
                            if face.geomType()!='CYLINDER':continue
                            surface=BRepAdaptor_Surface(face.wrapped,True);cyl=surface.Cylinder()
                            axis=_unit(cyl.Axis().Direction().Coord())
                            if abs(abs(_dot(axis,hole['axis']))-1)>tolerance:continue
                            if _distance(_axis_point(cyl.Location().Coord(),hole['axis']),hole['origin'])>tolerance:continue
                            u=(surface.FirstUParameter()+surface.LastUParameter())/2
                            point=surface.Value(u,(surface.FirstVParameter()+surface.LastVParameter())/2).Coord()
                            radial=_sub(_axis_point(point,hole['axis']),hole['origin'])
                            if _dot(face.normalAt(cq.Vector(*point)).toTuple(),radial)<=0:continue
                            ends=sorted(_dot(surface.Value(u,v).Coord(),hole['axis']) for v in
                                        [surface.FirstVParameter(),surface.LastVParameter()])
                            if ends[0]>hole['start']+tolerance or ends[1]<hole['end']-tolerance:continue
                            if abs(surface.LastUParameter()-surface.FirstUParameter()-2*math.pi)>tolerance:continue
                            if hole['complete'] and all(s['radius'] is not None for s in hole['segments']) and len({round(s['radius']/tolerance) for s in hole['segments']})==1:
                                thickness=cyl.Radius()-hole['diameter']/2
                                if thickness>tolerance:candidates.append(thickness)
                        if len(candidates)==1:row['measured'].extend(candidates)
                        else:row['issues'].append('radial_wall_boundary_ambiguous_or_incomplete')
                    row['method']='coaxial_complete_cylinder_radius_difference'
                    row['details']['scope']='radial cylindrical side wall; excludes end caps'
                else:
                    row['issues'].append('continuous_normal_wall_not_certified_by_analytic_measurements')
            else:
                row['issues'].append('measurement_kind_not_supported')
            if row['measured'] and not row['issues']:
                wanted = 0 if kind=='hole_position' else check['nominal']
                allowed = 0 if kind in {'solid_count','hole_count','void_connected'} else (check.get('tolerance_mm') or 0)+tolerance
                if kind == 'volume':
                    allowed = (check.get('tolerance_mm3') or 0)+row['details']['numerical_tolerance_mm3']
                row['outcome'] = 'passed' if all(abs(v-wanted)<=allowed for v in row['measured']) else 'failed'
            elif not row['issues']:
                row['issues'].append('no_measurement_evidence')
        except Exception as exc:
            row.update(outcome='indeterminate', measured=[], issues=[f'measurement_error:{type(exc).__name__}:{exc}'])
        evidence.append(row)
    return evidence
