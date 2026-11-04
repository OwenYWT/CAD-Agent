"""Inventory a complete rectilinear cavity/lid assembly from its final BRep.

All dimensions originate in boundaries. Reconstructing the observed material
with the enumerated tabs, ring groove and round passages proves whole-solid
coverage; unknown holes, splines, notches or missing material never disappear
behind a local probe or a nominal-driven component selector.
"""
import math


def _geometry():
    try:
        import feature_verification as g
    except ImportError:
        from sandbox import feature_verification as g
    return g


def _rectangle(wire, tolerance):
    """A complete four-edge planar rectangle, not just its bounding box."""
    edges=wire.Edges()
    if len(edges)!=4 or any(e.geomType()!='LINE' for e in edges):
        return False
    vectors=[(e.endPoint()-e.startPoint()).normalized() for e in edges]
    return all(abs(vectors[i].dot(vectors[(i+1)%4]))<=tolerance for i in range(4))


def _find_cavity(solid,tolerance):
    import cadquery as cq
    g=_geometry();faces=solid.Faces();found=[]
    for index,floor in enumerate(faces):
        if floor.geomType()!='PLANE' or floor.innerWires() or not _rectangle(floor.outerWire(),tolerance):
            continue
        normal=floor.normalAt();sides=[]
        for edge in floor.outerWire().Edges():
            adjacent=[(i,f) for i,f in enumerate(faces) if i!=index and any(e.wrapped.IsSame(edge.wrapped) for e in f.Edges())]
            if len(adjacent)!=1:break
            i,face=adjacent[0]
            if (face.geomType()!='PLANE' or abs(face.normalAt().dot(normal))>tolerance
                    or (face.Center()-floor.Center()).dot(normal)<=tolerance
                    or (floor.Center()-face.Center()).dot(face.normalAt())<=tolerance):break
            sides.append(i)
        if len(set(sides))!=4:continue
        height=max((v.Center()-floor.Center()).dot(normal) for v in solid.Vertices())
        void=cq.Solid.extrudeLinear(floor.outerWire(),[],normal*(height+10*tolerance))
        material=g._measurement_common(void,solid,'assembly_cavity_opening')
        if material is not None and material.Solids():continue
        found.append((index,floor,sides))
    return found


def measure_rectangular_assembly(shape,tolerance):
    import cadquery as cq
    try:
        from assembly_measurements import assembly_values
    except ImportError:
        from app.contracts.assembly_measurements import assembly_values
    g=_geometry()
    solids=shape.Solids()
    if len(solids)!=2 or not shape.isValid() or any(not s.isValid() for s in solids):
        raise ValueError('rectangular assembly requires one body and one separate valid lid')
    candidates=[(i,*c) for i,s in enumerate(solids) for c in _find_cavity(s,tolerance)]
    if len(candidates)!=1:
        raise ValueError('assembly has no unique complete open rectangular cavity')
    body_index,floor_index,floor,side_indices=candidates[0]
    edges=sorted(floor.outerWire().Edges(),key=lambda e:e.Length(),reverse=True)
    x=(edges[0].endPoint()-edges[0].startPoint()).normalized()
    if next(v for v in x.toTuple() if abs(v)>tolerance)<0:x=x*-1
    plane=cq.Plane(floor.Center(),x,floor.normalAt())
    body=plane.toLocalCoords(solids[body_index]);lid=plane.toLocalCoords(solids[1-body_index])
    local_floor=plane.toLocalCoords(floor)
    inner=list(g._geometric_bounds(local_floor));body_bounds=g._geometric_bounds(body)
    top=body_bounds[5];bottom=body_bounds[2]
    if top<=tolerance or bottom>=-tolerance:
        raise ValueError('cavity has no positive height or floor layer')
    planes=[]
    for f in body.Faces():
        if f.geomType()=='PLANE':
            n=f.normalAt().toTuple();axis=max(range(3),key=lambda j:abs(n[j]))
            if abs(abs(n[axis])-1)>tolerance:
                raise ValueError('unclassified oblique planar boundary in rectangular assembly')
            # Groove walls face the same direction as an exterior wall but
            # exist only near the rim. An opposed BODY wall must reach the
            # cavity floor plane; otherwise the groove rail would shrink the
            # body envelope and misclassify its material as mounting tabs.
            bounds=g._geometric_bounds(f)
            if bounds[2]<=tolerance and bounds[5]>tolerance:
                planes.append((axis,1 if n[axis]>0 else -1,f.Center().toTuple()[axis]))
        elif f.geomType()!='CYLINDER':
            raise ValueError('unclassified curved boundary in rectangular assembly')
    outer=[0.,0.,bottom,0.,0.,top]
    for axis in [0,1]:
        lower=[v for a,sign,v in planes if a==axis and sign==-1 and v<inner[axis]-tolerance]
        upper=[v for a,sign,v in planes if a==axis and sign==1 and v>inner[axis+3]+tolerance]
        if not lower or not upper:raise ValueError('complete opposed exterior walls are absent')
        outer[axis]=max(lower);outer[axis+3]=min(upper)
    inner[2]=0.;inner[5]=top

    def box(bounds):
        if any(b-a<=tolerance for a,b in zip(bounds[:3],bounds[3:])):
            raise ValueError('degenerate assembly material/void bounds')
        return cq.Solid.makeBox(*(b-a for a,b in zip(bounds[:3],bounds[3:])),cq.Vector(*bounds[:3]))

    def cut(a,b,stage):return g._measurement_boolean(a,b,stage,difference=True)
    def volume(s):return 0. if s is None else sum(abs(p.Volume()) for p in s.Solids())
    def assert_equal(a,b,stage):
        missing,extra=volume(cut(a,b,stage+'_missing')),volume(cut(b,a,stage+'_extra'))
        if missing or extra:
            raise ValueError(stage+': unaccounted material or void in complete assembly')
        return missing,extra

    envelope=box(outer);cavity=box(inner)
    outside=cut(body,envelope,'assembly_tab_inventory')
    tab_solids=[] if outside is None else outside.Solids()
    tabs=[];tab_boxes=[]
    for tab in tab_solids:
        bounds=list(g._geometric_bounds(tab));attachment=[]
        for axis in [0,1]:
            for side in [-1,1]:
                if abs(bounds[axis+3 if side==-1 else axis]-outer[axis if side==-1 else axis+3])<=tolerance:
                    other=1-axis
                    if bounds[other]>=outer[other]-tolerance and bounds[other+3]<=outer[other+3]+tolerance:
                        attachment.append((axis,side))
        if len(attachment)!=1 or bounds[2]<bottom-tolerance or bounds[5]>top+tolerance:
            raise ValueError('external material is not a uniquely attached rectangular mounting tab')
        axis,side=attachment[0]
        tabs.append({'bounds_mm':bounds,'attachment_axis':axis,'attachment_side':side})
        tab_boxes.append(box(bounds))

    grooves=[]
    for face in body.Faces():
        if (face.geomType()!='PLANE' or face.normalAt().z<1-tolerance or len(face.innerWires())!=1
                or not _rectangle(face.outerWire(),tolerance) or not _rectangle(face.innerWires()[0],tolerance)):
            continue
        outside_bounds=list(g._geometric_bounds(face.outerWire()))
        inside_bounds=list(g._geometric_bounds(face.innerWires()[0]))
        z=face.Center().z
        if z<=tolerance or z>=top-tolerance:continue
        if not all(outer[a]<outside_bounds[a]<inside_bounds[a]<inner[a] and
                   inner[a+3]<inside_bounds[a+3]<outside_bounds[a+3]<outer[a+3] for a in [0,1]):continue
        grooves.append({'outer_bounds_mm':outside_bounds,'inner_bounds_mm':inside_bounds,'floor_z_mm':z})
    if len(grooves)!=1:raise ValueError('a complete continuous rectangular sealing groove is absent or ambiguous')
    groove=grooves[0]
    groove_outer=[*groove['outer_bounds_mm'][:2],groove['floor_z_mm'],*groove['outer_bounds_mm'][3:5],top]
    groove_inner=[*groove['inner_bounds_mm'][:2],groove['floor_z_mm'],*groove['inner_bounds_mm'][3:5],top]
    groove_void=cut(box(groove_outer),box(groove_inner),'assembly_groove_ring')

    boundary=[];passages=g._cylindrical_voids(body,tolerance,boundary_faces=boundary)
    if any(not p['complete'] or abs(p['diameter']-p['largest_section_diameter'])>tolerance for p in passages):
        raise ValueError('assembly round passage has incomplete or varying cross-section')
    exterior,_=g._exterior_void(body,tolerance)
    mounting=[];connectors=[];hole_tools=[];accounted=set()
    for p in passages:
        axis=p['axis'];origin=p['origin']
        start=[o+a*p['start'] for o,a in zip(origin,axis)]
        tool=cq.Solid.makeCylinder(p['diameter']/2,p['end']-p['start'],cq.Vector(*start),cq.Vector(*axis))
        through,through_proof=g._through_passage(body,p,tolerance,exterior)
        row={'diameter_mm':p['diameter'],'axis':list(axis),'origin_mm':list(origin),
             'interval_mm':[p['start'],p['end']],'through':through,'through_proof':through_proof,
             'face_indices':[s['face_index'] for s in p['segments']]}
        if abs(axis[2]-1)<=tolerance:
            selected=[i for i,t in enumerate(tabs) if all(
                t['bounds_mm'][a]<origin[a]<t['bounds_mm'][a+3] for a in [0,1])]
            if len(selected)!=1:raise ValueError('a vertical passage is not owned by one external mounting tab')
            row['tab_index']=selected[0]
            row['cavity_clearance_mm']=tool.distance(cavity)
            mounting.append(row)
        elif abs(axis[0]-1)<=tolerance or abs(axis[1]-1)<=tolerance:
            a=0 if axis[0]>.5 else 1;other=1-a
            middle=(p['start']+p['end'])/2
            if not (outer[other]<origin[other]<outer[other+3] and bottom<origin[2]<top
                    and (outer[a]-tolerance<=middle<=inner[a]+tolerance or
                         inner[a+3]-tolerance<=middle<=outer[a+3]+tolerance)):
                raise ValueError('a horizontal passage is not in an exterior cavity wall')
            connectors.append(row)
        else:raise ValueError('unclassified oblique passage in assembly')
        accounted.update(row['face_indices']);hole_tools.append(tool)
    if len(connectors)!=1:
        raise ValueError('the side connector passage is absent or ambiguous')
    if any(f['surface_type']=='CYLINDER' and f['face_index'] not in accounted for f in boundary):
        raise ValueError('unaccounted cylindrical assembly boundary')
    # Enumerate all missing material explicitly. Requested dimensions never
    # enter reconstruction; undeclared channels/bores leave a positive residual.
    reconstructed=cut(envelope,cavity,'assembly_shell')
    for tab_box in tab_boxes:reconstructed=reconstructed.fuse(tab_box)
    reconstructed=cut(reconstructed,groove_void,'assembly_groove')
    for tool in hole_tools:reconstructed=cut(reconstructed,tool,'assembly_passage')
    body_missing,body_extra=assert_equal(reconstructed,body,'assembly_body_coverage')
    lid_bounds=list(g._geometric_bounds(lid))
    lid_missing,lid_extra=assert_equal(box(lid_bounds),lid,'assembly_lid_coverage')
    overlap=volume(g._measurement_common(body,lid,'assembly_component_overlap'))
    faces=shape.Faces()
    cert={'schema_version':'rectangular-assembly.v1','complete':True,'selection_uses_nominal':False,
        'frame':{'origin_mm':list(floor.Center().toTuple()),'x_axis':list(plane.xDir.toTuple()),
                 'y_axis':list(plane.yDir.toTuple()),'z_axis':list(plane.zDir.toTuple())},
        'body_bounds_mm':outer,'cavity_bounds_mm':inner,'lid_bounds_mm':lid_bounds,
        'tabs':tabs,'mounting_holes':mounting,'connector':connectors[0],'groove':groove,
        'coverage':{'solid_count':2,'face_count':len(faces),'covered_face_indices':list(range(len(faces))),
            'face_types':[f.geomType() for f in faces],'body_missing_mm3':body_missing,'body_extra_mm3':body_extra,
            'lid_missing_mm3':lid_missing,'lid_extra_mm3':lid_extra,'component_overlap_mm3':overlap,
            'body_volume_mm3':body.Volume(),'lid_volume_mm3':lid.Volume(),
            'method':'complete_observed_material_reconstruction_with_enumerated_voids'}}
    cert['values']=assembly_values(cert,tolerance)
    return cert
