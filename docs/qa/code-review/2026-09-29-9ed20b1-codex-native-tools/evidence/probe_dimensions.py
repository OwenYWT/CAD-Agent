import json
import tempfile
from pathlib import Path
import cadquery as cq
from feature_verification import measure_checks
from geometry_validation import validate_geometry_files

box = cq.Solid.makeBox(20, 20, 20, cq.Vector(-10,-10,0))
cross = cq.Solid.makeCylinder(1, 25, cq.Vector(-12.5,0,10), cq.Vector(1,0,0))
tube = cq.Workplane('XY').circle(10).circle(8).extrude(20).val()
shapes = [('box',box), ('sphere',cq.Solid.makeSphere(10,angleDegrees1=-90)),
          ('tube',tube), ('cross_box',box.cut(cross)), ('cross_tube',tube.cut(cross))]
rows=[]
for name,shape in shapes:
    contract={'objective':'20 mm in X, Y, Z', 'checks':[
        {'check_id':str(i),'kind':'overall_dimension','nominal':20,'required':True,
         'source_quote':'20 mm','description':'nominal extent','scope':{'axis':axis}}
        for i,axis in enumerate([[1,0,0],[0,1,0],[0,0,1]])]}
    with tempfile.TemporaryDirectory() as directory:
        p=Path(directory)/'model.step';cq.exporters.export(shape,str(p))
        report=validate_geometry_files([{'role':'model','format':'step','path':str(p)}],acceptance=contract)
    rows.append({'shape':name,'valid':shape.isValid(),'direct':measure_checks(shape,contract['checks']),
        'step_report':report})
print('DIMENSION_REVIEW='+json.dumps(rows))
