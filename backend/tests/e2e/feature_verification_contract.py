"""Independent analytic BRep oracles, executed in the actual kernel image."""
import json
import tempfile
from pathlib import Path
import cadquery as cq

from feature_verification import measure_checks


def roundtrip(shape):
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'final.step'
        cq.exporters.export(shape,str(path))
        return cq.importers.importStep(str(path)).val()


def check(kind, nominal=None, **scope):
    return dict(check_id=kind, kind=kind, nominal=nominal, scope=scope)


for size, diameter, depth in [(40, 4, 7), (60, 8, 13), (90, 6, 17)]:
    block = cq.Workplane("XY").box(size,size,depth,centered=(True,True,False))
    positions = [(-size/4,0), (size/4,0)]
    part = block.faces(">Z").workplane().pushPoints(positions).hole(diameter).val()
    requests = [check('solid_count',1), check('hole_count',2,axis=[0,0,1]),
                check('hole_diameter',diameter,axis=[0,0,1]),
                check('hole_depth',depth,axis=[0,0,1]),
                check('hole_position',axis=[0,0,1],centers_mm=[[x,y,0] for x,y in positions]),
                check('overall_dimension',depth,axis=[0,0,1])]
    evidence=measure_checks(part,requests)
    assert all(e['outcome']=='passed' for e in evidence),json.dumps(evidence)
    bad=measure_checks(part,[check('hole_count',3),check('hole_depth',depth+1),check('hole_diameter',diameter+1)])
    assert all(e['outcome']=='failed' for e in bad),bad
    # Full rotation/translation must preserve intrinsic measurements.
    shifted=part.rotate((0,0,0),(0,1,0),90).translate((11,-9,23))
    evidence=measure_checks(shifted,[check('hole_count',2,axis=[1,0,0]),
        check('hole_depth',depth,axis=[1,0,0]),check('overall_dimension',depth,axis=[1,0,0])])
    assert all(e['outcome']=='passed' for e in evidence),evidence

# An outside cylinder must not be counted as a hole.
boss=cq.Workplane('XY').circle(5).extrude(12).val()
assert measure_checks(boss,[check('hole_count',0)])[0]['outcome']=='passed'
# A stepped entrance is one bore, not one hole for every cylindrical face.
stepped=cq.Workplane('XY').box(30,30,12).faces('>Z').workplane().cboreHole(4,8,3).val()
assert measure_checks(stepped,[check('hole_count',1)])[0]['outcome']=='passed'
assert measure_checks(stepped,[check('hole_depth',12)])[0]['outcome']=='passed'
sink=cq.Workplane('XY').box(30,30,12).faces('>Z').workplane().cskHole(4,8,90).val()
assert measure_checks(sink,[check('hole_depth',12)])[0]['outcome']=='passed'
assert measure_checks(sink,[check('hole_depth',10,hole_depth_mode='shaft_length')])[0]['outcome']=='passed'
assert measure_checks(stepped,[check('hole_depth',9,hole_depth_mode='shaft_length')])[0]['outcome']=='passed'
assert measure_checks(sink,[check('hole_diameter',8,hole_diameter_mode='largest_section')])[0]['outcome']=='passed'
assert measure_checks(sink,[check('hole_diameter',4,hole_diameter_mode='largest_section')])[0]['outcome']=='failed'
blind=cq.Workplane('XY').box(30,30,12).faces('>Z').workplane().hole(4,5).val()
assert measure_checks(blind,[check('hole_depth',5)])[0]['outcome']=='passed'
# A disconnected lip cannot pass a one-piece requirement.
one=cq.Workplane('XY').box(24,16,3).val()
two=cq.Compound.makeCompound([one,one.translate((0,0,4))])
assert measure_checks(two,[check('solid_count',1)])[0]['outcome']=='failed'
assert measure_checks(two,[check('solid_count',2)])[0]['outcome']=='passed'
for wall in [1.5,3,5]:
    tube=cq.Workplane('XY').circle(20).circle(20-wall).extrude(30).val()
    measured=measure_checks(tube,[check('wall_thickness',wall,axis=[0,0,1],wall_mode='radial')])
    assert measured[0]['outcome']=='passed',measured
    assert measure_checks(tube,[check('wall_thickness',wall+1,axis=[0,0,1],wall_mode='radial')])[0]['outcome']=='failed'
    # The radius difference is not a material certificate. Neither a hidden
    # cavity nor a cross-drilled wall may retain a passing radial certificate.
    cavity=cq.Solid.makeSphere(wall/4,cq.Vector(20-wall/2,0,15),angleDegrees1=-90)
    drill=cq.Solid.makeCylinder(wall/3,50,cq.Vector(-25,0,15),cq.Vector(1,0,0))
    for broken in [tube.cut(cavity),tube.cut(drill)]:
        assert broken.isValid() and len(broken.Solids())==1
        assert tube.cut(broken).Volume()>0
        for final in [broken,roundtrip(broken)]:
            assert measure_checks(final,[check('wall_thickness',wall,axis=[0,0,1],wall_mode='radial')])[0]['outcome']!='passed'
    rotated=tube.rotate((0,0,0),(0,1,0),63).translate((8,-11,6))
    import math
    axis=[math.sin(math.radians(63)),0,math.cos(math.radians(63))]
    assert measure_checks(rotated,[check('wall_thickness',wall,axis=axis,wall_mode='radial')])[0]['outcome']=='passed'

# Intersections may inflate a conservative bounding box; nominal extents must
# use geometric extrema and their numerical uncertainty, not a design offset.
for radius,height in [(10,20),(17,31),(6,9)]:
    tube=cq.Workplane('XY').circle(radius).circle(radius-2).extrude(height).val()
    drill=cq.Solid.makeCylinder(0.6,4*radius,cq.Vector(-2*radius,0,height/2),cq.Vector(1,0,0))
    drilled=roundtrip(tube.cut(drill))
    for axis,extent in [([1,0,0],2*radius),([0,1,0],2*radius),([0,0,1],height)]:
        measured=measure_checks(drilled,[check('overall_dimension',extent,axis=axis)])[0]
        assert measured['outcome']=='passed',(radius,height,axis,measured)
        assert measure_checks(drilled,[check('overall_dimension',extent+0.001,axis=axis)])[0]['outcome']=='failed'
slab=cq.Workplane('XY').box(24,16,3).val()
local=measure_checks(slab,[check('wall_thickness',3,point_mm=[0,0,0],axis=[0,0,1],wall_mode='local_probe')])
assert local[0]['outcome']=='passed',local
whole=measure_checks(slab,[check('wall_thickness',3)])[0]
assert whole['outcome']=='passed' and whole['method']=='whole_part_certified_normal_layer_coverage',whole
plane=measure_checks(slab,[check('wall_thickness',3,point_mm=[0,0,1.5],wall_mode='surface_normal')])
assert plane[0]['outcome']=='passed',plane
# A hidden cavity defeats the complete material-prism proof, even when both
# exterior planes are still exactly three millimetres apart.
hidden=slab.cut(cq.Workplane('XY').box(2,2,1).val())
assert measure_checks(hidden,[check('wall_thickness',3,point_mm=[6,0,1.5],wall_mode='surface_normal')])[0]['outcome']!='passed'
assert measure_checks(hidden,[check('wall_thickness',3)])[0]['outcome']!='passed'

# The complete STEP validation path must enforce, retain and bind measurements.
import tempfile
from geometry_validation import validate_geometry_files
from geometry_request import geometry_request_digest
retained=Path(__file__).parent/'fixtures/native_failures/disconnected_lip.step'
old=validate_geometry_files([{'role':'final','format':'step','path':str(retained)}])
strict=validate_geometry_files([{'role':'final','format':'step','path':str(retained)}],expected_solid_count=1)
assert old['outcome']=='passed' and strict['outcome']=='failed'
assert strict['artifacts'][0]['solid_count']==2
block=cq.Workplane('XY').box(30,20,10,centered=(True,True,False))
separate=block.faces('>Z').workplane().pushPoints([(-7,0),(7,0)]).hole(4).val()
channel=cq.Workplane('XY').box(14,2,2).translate((0,0,5)).val()
joined=separate.cut(channel)
scope={'region_min_mm':[-15,-10,0],'region_max_mm':[15,10,10],'centers_mm':[[-7,0,5],[7,0,5]]}
for part,wanted in [(separate,0),(joined,1)]:
    result=measure_checks(part,[check('void_connected',wanted,**scope)])
    assert result[0]['outcome']=='passed',result
with tempfile.TemporaryDirectory() as directory:
    file=Path(directory)/'candidate.step'
    cq.exporters.export(stepped,str(file))
    contract={'objective':'one hole, depth 12 mm',
        'checks':[{'check_id':'depth','kind':'hole_depth','nominal':12,
        'required':True,'description':'total axial recess','source_quote':'depth 12 mm'}]}
    report=validate_geometry_files(
        [{'role':'model','format':'step','path':str(file)}], expected_solid_count=1,
        acceptance=contract)
    assert report['outcome']=='passed',report
    assert report['request_sha256']==geometry_request_digest(
        expected_dimensions_mm={},dimension_tolerance=0.05,expected_solid_count=1,
        acceptance=contract)
    contract['checks'][0]['nominal']=13
    report=validate_geometry_files([{'role':'model','format':'step','path':str(file)}],acceptance=contract)
    assert report['outcome']=='failed',report
print('FEATURE_MEASUREMENT_CONTRACT_PASSED')
