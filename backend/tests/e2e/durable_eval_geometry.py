"""Independent fixed-case assertions over the actual live FCStd and STEP files."""
import hashlib
import json
import math
from pathlib import Path
import subprocess

import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Surface

root = Path('/sandbox/input')
expected = json.loads((root/'expected.json').read_text())
before = {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ('model.step','model.FCStd')}
shape = cq.importers.importStep(str(root/'model.step')).val()
assert shape.isValid() and len(shape.Solids()) == expected['solid_count']
bounds = shape.BoundingBox()
for actual, name in zip((bounds.xlen,bounds.ylen,bounds.zlen), ('length','width','thickness')):
    assert abs(actual-expected[name]) < 1e-5, (name,actual,expected[name])
faces = [face for face in shape.Faces() if face.geomType()=='CYLINDER']
assert len(faces)==expected['hole_count'], len(faces)
for face in faces:
    cylinder = BRepAdaptor_Surface(face.wrapped).Cylinder()
    point = cylinder.Location()
    assert abs(cylinder.Radius()*2-expected['hole_diameter']) < 1e-5
    assert abs(abs(cylinder.Axis().Direction().Z())-1) < 1e-7
    assert abs(point.X()-(bounds.xmin+bounds.xmax)/2) < 1e-5
    assert abs(point.Y()-(bounds.ymin+bounds.ymax)/2) < 1e-5
    assert abs(face.BoundingBox().zlen-expected['thickness']) < 1e-5
volume = expected['length']*expected['width']*expected['thickness'] - math.pi*(expected['hole_diameter']/2)**2*expected['thickness']
assert abs(shape.Volume()-volume) < 1e-4
native = subprocess.run(['/opt/freecad/bin/FreeCADCmd','-c',
    "import FreeCAD as A; d=A.openDocument('/sandbox/input/model.FCStd'); d.recompute(); "
    "assert d.Objects; assert all(not o.State or list(o.State)==['Up-to-date'] for o in d.Objects); "
    "print('CAD_EVAL_NATIVE_READABLE=1'); A.closeDocument(d.Name)"], capture_output=True,text=True)
assert native.returncode==0 and 'CAD_EVAL_NATIVE_READABLE=1' in native.stdout and 'Traceback' not in native.stdout+native.stderr, native.stdout+native.stderr
assert before == {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in before}
print('CAD_DURABLE_EVAL_GEOMETRY='+json.dumps({'expected':expected,'volume_mm3':shape.Volume(),
    'artifact_sha256s':before,'native_readable':True,'status':'passed'}))
