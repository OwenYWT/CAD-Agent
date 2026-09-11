"""Measurements on real analytic B-rep faces, not triangle edge lengths.

Bridge span covers planar horizontal roofs in the supplied model's +Z build
orientation. Unsupported one-sided roofs are measured separately; curved
down-facing regions remain part of the mesh overhang check.
"""
from __future__ import annotations

import math


def measure_brep(shape) -> dict[str,float|int]:
    import cadquery as cq
    from OCP.BRepTools import BRepTools

    solids=shape.Solids()
    if not solids:
        raise ValueError("DFM requires a solid B-rep")
    faces=shape.Faces()
    if len(faces)>5000:
        raise ValueError("DFM analytic face limit exceeded")
    sizes=[];roof_spans=[];cantilever_area=0.0;roof_count=0
    bed=shape.BoundingBox().zmin

    def supported(point):
        return any(s.isInside(point,1e-6) for s in solids)

    for face in faces:
        kind=face.geomType()
        surface=face._geomAdaptor()
        if kind=="CYLINDER":
            sizes.append(2*surface.Cylinder().Radius())
        elif kind=="SPHERE":
            sizes.append(2*surface.Sphere().Radius())
        elif kind=="TORUS":
            sizes.append(2*surface.Torus().MinorRadius())
        elif kind=="PLANE":
            u0,u1,v0,v1=BRepTools.UVBounds_s(face.wrapped)
            sizes.extend(x for x in (abs(u1-u0),abs(v1-v0)) if x>1e-6)
        else:
            continue
        bounds=face.BoundingBox()
        if kind!="PLANE" or bounds.zmax-bounds.zmin>1e-6 or bounds.zmin<=bed+1e-5:
            continue
        if face.normalAt().z>-.999:
            continue
        roof_count+=1
        vertices=[v.toTuple() for v in face.Vertices()]
        directions={(1.0,0.0),(0.0,1.0)}
        for edge in face.Edges():
            if edge.geomType()=="LINE":
                vector=edge.endPoint()-edge.startPoint();length=math.hypot(vector.x,vector.y)
                if length>1e-6:
                    x,y=vector.x/length,vector.y/length
                    if x<0 or (abs(x)<1e-8 and y<0):x,y=-x,-y
                    directions.add((round(x,8),round(y,8)))
        if len(directions)>32:
            raise ValueError("DFM bridge direction limit exceeded")
        candidates=[]
        for dx,dy in sorted(directions):
            dots=[x*dx+y*dy for x,y,_ in vertices]
            crosses=[-x*dy+y*dx for x,y,_ in vertices]
            if not dots or max(crosses)-min(crosses)<1e-6:
                continue
            spans=[];unanchored=False
            for fraction in (0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9):
                cross=min(crosses)+(max(crosses)-min(crosses))*fraction
                def point(along,z):
                    return (dx*along-dy*cross,dy*along+dx*cross,z)
                line=cq.Edge.makeLine(cq.Vector(*point(min(dots)-1,bounds.zmin)),cq.Vector(*point(max(dots)+1,bounds.zmin)))
                for segment in face.intersect(line).Edges():
                    a,b=segment.startPoint(),segment.endPoint()
                    low,high=sorted((a.x*dx+a.y*dy,b.x*dx+b.y*dy))
                    if high-low<1e-6:continue
                    if supported(point(low-1e-4,bounds.zmin-1e-4)) and supported(point(high+1e-4,bounds.zmin-1e-4)):
                        spans.append(high-low)
                    else:
                        unanchored=True
            if spans and not unanchored:
                candidates.append(max(spans))
        if candidates:
            roof_spans.append(min(candidates))
        else:
            cantilever_area+=face.Area()
    measured={
        "horizontal_roof_count":roof_count,
        "bridge_span_mm":max(roof_spans,default=0.0),
        "unanchored_roof_area_mm2":cantilever_area,
    }
    positive=[float(size) for size in sizes if math.isfinite(size) and size>1e-6]
    if positive:
        measured["min_feature_size_mm"]=min(positive)
    return measured
