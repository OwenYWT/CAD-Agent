"""Independent final STEP tests, using analytic extrema as the reference.

The nonuniformly scaled sphere becomes a NURBS ellipsoid. Its inner and outer
semiaxes differ by (2, 3, 4), so the minimum surface separation is 2 mm, including
away from the selected face-identification points on the y axis.
"""
import json
import sys
import tempfile
from pathlib import Path

import cadquery as cq

sys.path.insert(0, '/opt/cad-agent')
from feature_verification import measure_checks


def check(distance, points):
    return {'check_id': 'surfaces', 'kind': 'surface_clearance', 'nominal': distance,
            'scope': {'centers_mm': points}}


reports = []
outer = cq.Workplane('XY').sphere(10).val()
inner = cq.Workplane('XY').sphere(8).val()
shell = outer.cut(inner)
for shape, points, wanted in [
    (shell, [[0, 10, 0], [0, 8, 0]], 2),
    (outer.cut(inner.translate((0.5, 0, 0))), [[0, 10, 0], [0.5, 8, 0]], 1.5),
    (shell.transformGeometry(cq.Matrix([[1,0,0,0], [0,1.5,0,0], [0,0,2,0], [0,0,0,1]])),
     [[0,15,0], [0,12,0]], 2),
]:
    assert shape.isValid() and len(shape.Solids()) == 1
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'final.step'
        cq.exporters.export(shape, str(path))
        final = cq.importers.importStep(str(path)).val()
        evidence = measure_checks(final, [check(wanted, points)])[0]
        assert evidence['outcome'] == 'passed', evidence
        assert evidence['details']['normal_wall_thickness'] == 'unverified'
        assert measure_checks(final, [check(wanted+0.5, points)])[0]['outcome'] == 'failed'
        reports.append(evidence)
assert 'BSPLINE' in reports[-1]['details']['face_geometry'], reports[-1]
# An offset cavity makes the minimum wall smaller elsewhere, even though the
# identification probes remain near the thick side. No probe-only certificate.
assert reports[1]['measured'][0] < 2
for points in [[[0,10,0],[0,10,0]], [[10,0,0],[8,0,0]], [[0,11,0],[0,8,0]]]:
    assert measure_checks(shell,[check(2,points)])[0]['outcome']=='indeterminate'

def normal_check(thickness, point):
    return {'check_id':'wall', 'kind':'wall_thickness', 'nominal':thickness,
            'scope':{'wall_mode':'surface_normal','point_mm':point}}


def entire_wall(thickness):
    return {'check_id':'entire-wall','kind':'wall_thickness','nominal':thickness,
            'scope':{'wall_mode':'continuous_normal'}}

for geometry, point in [
    (shell, [0,10,0]),
    (cq.Solid.makeTorus(20,5).cut(cq.Solid.makeTorus(20,3)), [0,25,0]),
]:
    evidence = measure_checks(geometry,[normal_check(2,point)])[0]
    assert evidence['outcome']=='passed',evidence
    assert evidence['method']=='analytic_normal_offset_and_whole_material_layer'
    reports.append(evidence)
    assert measure_checks(geometry,[normal_check(3,point)])[0]['outcome']=='failed'
    complete=measure_checks(geometry,[entire_wall(2)])[0]
    assert complete['outcome']=='passed',complete
    assert complete['method']=='whole_part_certified_normal_layer_coverage'
    reports.append(complete)
# Correct boundary radii cannot hide missing material within the entire layer.
hidden = shell.cut(cq.Workplane('XY').sphere(0.3).translate((0,0,9)).val())
assert measure_checks(hidden,[normal_check(2,[0,10,0])])[0]['outcome']=='indeterminate'
eccentric = outer.cut(inner.translate((0.5,0,0)))
assert measure_checks(eccentric,[normal_check(2,[0,10,0])])[0]['outcome']=='indeterminate'
assert measure_checks(hidden,[entire_wall(2)])[0]['outcome']!='passed'
assert measure_checks(eccentric,[entire_wall(2)])[0]['outcome']!='passed'

# Freeform trimmed surface with a varying normal field. The independently
# supplied solid is reimported from STEP before its whole-face certificate runs.
from OCP.Geom import Geom_BezierSurface
from OCP.gp import gp_Pnt
from OCP.TColgp import TColgp_Array2OfPnt
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeThickSolid
poles=TColgp_Array2OfPnt(1,3,1,3)
for i,x in enumerate([-10,0,10],1):
    for j,y in enumerate([-10,0,10],1):
        poles.SetValue(i,j,gp_Pnt(x,y,[2,-2,2][i-1]+[2,-2,2][j-1]))
face=cq.Face(BRepBuilderAPI_MakeFace(Geom_BezierSurface(poles),1e-7).Face())
for thickness in (1.5,2,3):
    maker=BRepOffsetAPI_MakeThickSolid()
    maker.MakeThickSolidBySimple(face.wrapped,-thickness)
    curved=cq.Shape.cast(maker.Shape())
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'freeform.step'
        cq.exporters.export(curved,str(path))
        final=cq.importers.importStep(str(path)).val()
        evidence=measure_checks(final,[normal_check(thickness,[0,0,0])])[0]
        assert evidence['outcome']=='passed',evidence
        assert evidence['method']=='whole_curved_face_offset_boundary_and_material_layer'
        assert evidence['details']['self_intersections_checked'] is True
        assert measure_checks(final,[normal_check(thickness+0.5,[0,0,0])])[0]['outcome']=='failed'
        reports.append(evidence)
        hidden=final.cut(cq.Workplane('XY').sphere(0.15).translate((4,0,-0.7)).val())
        assert hidden.Volume()<final.Volume()
        assert measure_checks(hidden,[normal_check(thickness,[0,0,0])])[0]['outcome']=='indeterminate'
        moved=final.rotate((0,0,0),(1,2,3),37).translate((11,-7,9))
        assert measure_checks(moved,[normal_check(thickness,[11,-7,9])])[0]['outcome']=='passed'
        whole=measure_checks(final,[entire_wall(thickness)])[0]
        assert whole['outcome']=='passed',whole
        assert whole['details']['uncovered_material_mm3']<=whole['details']['boolean_volume_tolerance_mm3']
        assert measure_checks(final,[entire_wall(thickness+0.5)])[0]['outcome']=='failed'
        assert measure_checks(moved,[entire_wall(thickness)])[0]['outcome']=='passed'
        assert measure_checks(hidden,[entire_wall(thickness)])[0]['outcome']!='passed'
        reports.append(whole)
# Independently constructed variable thickness cannot pass using the thinner
# region alone, and extra material cannot disappear behind a local certificate.
thin=cq.Solid.makeBox(20,20,2)
thick=cq.Solid.makeBox(20,20,4,pnt=cq.Vector(20,0,0))
variable=thin.fuse(thick)
assert measure_checks(variable,[entire_wall(2)])[0]['outcome']!='passed'
plate=measure_checks(thin,[entire_wall(2)])[0]
assert plate['outcome']=='passed',plate
disjoint=cq.Compound.makeCompound([thin,thick.translate((40,0,0))])
assert measure_checks(disjoint,[entire_wall(2)])[0]['outcome']!='passed'
print('CURVED_SURFACE_CONTRACT='+json.dumps({'status':'passed','evidence':reports}))
