"""Actual FCStd reopen, STEP measurement and source-bound material predicates."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from geometry_validation import validate_geometry_files


cases = json.loads(Path('/sandbox/input/intake-semantics.json').read_text())
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    program = f'''
import FreeCAD as App, Part, json
root={str(root)!r}
def box(height=35, wall=3, bottom=3, right=None):
    right=wall if right is None else right
    return Part.makeBox(90,60,height).cut(Part.makeBox(90-wall-right,60-2*wall,height-bottom,App.Vector(wall,wall,bottom)))
shapes={{'box_good':box(), 'box_wrong_wall':box(3,1,1), 'box_one_thin_wall':box(right=2),
    'box_wrong_floor':box(bottom=1), 'box_rotated':box(), 'box_solid':Part.makeBox(90,60,35),
    'box_closed':Part.makeBox(90,60,35).cut(Part.makeBox(84,54,29,App.Vector(3,3,3))),
    'box_two_cavities':Part.makeBox(90,60,35).cut(Part.makeBox(40,54,32,App.Vector(3,3,3))).cut(Part.makeBox(41,54,32,App.Vector(46,3,3))),
    'cylinder_good':Part.makeCylinder(20,25), 'cylinder_wrong_radius':Part.makeCylinder(21,25),
    'cylinder_wrong_height':Part.makeCylinder(20,26), 'cylinder_rotated':Part.makeCylinder(20,25),
    'cylinder_box':Part.makeBox(40,40,25), 'cylinder_step':Part.makeCylinder(20,20).fuse(Part.makeCylinder(19,5,App.Vector(0,0,20))),
    'cylinder_bore':Part.makeCylinder(20,25).cut(Part.makeCylinder(3,25)),
    'sphere_good':Part.makeSphere(20), 'sphere_wrong':Part.makeSphere(21), 'sphere_box':Part.makeBox(40,40,40),
    'cube_good':Part.makeBox(100,100,100), 'cube_rotated':Part.makeBox(100,100,100),
    'cube_wrong_size':Part.makeBox(101,101,101), 'cube_small':Part.makeBox(25,25,25),
    'cube_rectangular':Part.makeBox(100,50,100),
    'cube_hollow':Part.makeBox(100,100,100).cut(Part.makeBox(98,98,98,App.Vector(1,1,1))),
    'cube_notch':Part.makeBox(100,100,100).cut(Part.makeBox(1,1,1))}}
base=Part.makePolygon([App.Vector(0,0,0),App.Vector(100,0,0),App.Vector(100,100,0),App.Vector(0,100,0),App.Vector(0,0,0)])
shapes['cube_sheared']=Part.Face(base).extrude(App.Vector(10,0,100))
for suffix in ['micro','nurbs']:
    tool=Part.makeCylinder(.01,.5,App.Vector(0,30,15),App.Vector(1,0,0))
    if suffix=='nurbs':tool=tool.toNurbs()
    shapes['cube_blind_'+suffix]=shapes['cube_good'].cut(tool)
    shapes['box_blind_'+suffix]=box().cut(tool)
    for kind,position,direction in [('inner',App.Vector(2.5,30,15),App.Vector(1,0,0)),
                                    ('floor',App.Vector(45,30,2.5),App.Vector(0,0,1))]:
        tool=Part.makeCylinder(.01,.5,position,direction)
        if suffix=='nurbs':tool=tool.toNurbs()
        shapes['box_'+kind+'_blind_'+suffix]=box().cut(tool)
shapes['box_floor_through']=box().cut(Part.makeCylinder(.5,3,App.Vector(45,30,0)))
shapes['box_upper_wall_recess']=box().cut(Part.makeBox(1,20,15,App.Vector(2,20,20)))
shapes['box_stepped_cavity']=box().cut(Part.makeBox(86,56,15,App.Vector(2,2,20)))
shapes['cylinder_blind_nurbs']=Part.makeCylinder(20,25).cut(Part.makeCylinder(.01,4,App.Vector(5,0,21)).toNurbs())
for name in ['box_rotated','cylinder_rotated','cube_rotated']:
    shapes[name].rotate(App.Vector(),App.Vector(1,2,3),37)
    shapes[name].translate(App.Vector(17,-9,11))
bop_checks={{}}
for name,shape in shapes.items():
    try:
        shape.check(True)
        bop_checks[name]={{'status':'passed'}}
    except ValueError as error:
        bop_checks[name]={{'status':'failed','diagnostic':str(error)}}
    assert shape.isValid() and len(shape.Solids)==1, name
    doc=App.newDocument(name)
    body=doc.addObject('PartDesign::Body','Body')
    feature=body.newObject('PartDesign::Feature','MeasuredSource');feature.Shape=shape
    doc.recompute();doc.saveAs(root+'/'+name+'.FCStd');App.closeDocument(doc.Name)
    reopened=App.openDocument(root+'/'+name+'.FCStd')
    assert reopened.Body.Shape.isValid(), name
    Part.export([reopened.Body],root+'/'+name+'.step')
    App.closeDocument(reopened.Name)
with open(root+'/native-bop-checks.json','w') as stream:
    json.dump(bop_checks,stream,indent=2)
print('CAD_MATERIAL_NATIVE_READY')
'''
    native_script = root / 'material-native.py'
    native_script.write_text(program)
    result = subprocess.run(['/opt/freecad/bin/FreeCADCmd','-c',
        f'exec(compile(open({str(native_script)!r}).read(), {str(native_script)!r}, "exec"))'],
        capture_output=True,text=True,timeout=90)
    if os.environ.get('CAD_INTAKE_EVIDENCE_DIR'):
        retained = Path(os.environ['CAD_INTAKE_EVIDENCE_DIR']) / 'saved-models'
        shutil.copytree(root, retained)
        retained.chmod(0o755)
    assert result.returncode == 0 and 'CAD_MATERIAL_NATIVE_READY' in result.stdout, result.stdout+result.stderr
    bop_checks = json.loads((root/'native-bop-checks.json').read_text())
    # Ordinary positives and the same-bounds counterexamples must be strict
    # valid geometry. Adversarial NURBS inputs retain their kernel diagnostics
    # and still must be rejected by the independent acceptance gate below.
    strict_models = {c['model'] for c in cases if c['expected'] in {'passed', 'failed'}}
    strict_models.update({'cube_rectangular', 'sphere_box', 'cylinder_box'})
    assert all(bop_checks[name]['status'] == 'passed' for name in strict_models), bop_checks
    rows = []
    for case in cases:
        path = root / (case['model']+'.step')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        context = {'schema_version':'measurement-context.v1','base_revision_id':'source',
                   'checkpoint_manifest_id':'intake-materials','artifact_sha256s':[digest]}
        report = validate_geometry_files([{'role':'model','format':'step','path':str(path)}],
            acceptance=case['contract'],measurement_context=context)
        assert report['outcome'] == case['expected'], (case['model'], case['expected'], report)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        rows.append({**case,'report':report,'context':context})
    print('CAD_INTAKE_SEMANTICS='+json.dumps(rows,ensure_ascii=False))
