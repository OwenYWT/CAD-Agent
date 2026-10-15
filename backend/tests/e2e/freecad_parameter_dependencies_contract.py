"""Real native edit counterexamples; preserve FCStd and STEP for inspection."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, '/opt/cad-agent')
import FreeCAD as App
import Part
import freecad_entry as runner

root = Path('/sandbox/output')
fixture = json.loads(Path('/sandbox/input/parameter-dependency.json').read_text())
evidence = []


def execute(plan, base=None):
    return runner.run_task({'schema_version': 'mcad-capability-task.v1', 'capability': 'freecad',
        'operation': 'execute', 'inputs': {'base': base} if base else {}, 'params': {'plan': plan}})


def mutation(ops, name):
    return {'schema_version': 'freecad-operation-plan.v1', 'document_name': name,
        'operations': [*ops, {'op_id': name+'-export', 'action': 'document.export',
            'args': {'formats': ['fcstd', 'step'], 'basename': name}}]}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


plate = execute(fixture['plate'])
doc = App.openDocument(plate['files']['fcstd'])
doc.Body.Placement.Base = App.Vector(13, -7, 11)
doc.recompute()
width = next(i for i,c in enumerate(doc.BaseSketch.Constraints) if c.Type=='Distance' and abs(c.Value-60)<1e-7)
doc.saveAs('/sandbox/input/plate.FCStd'); App.closeDocument(doc.Name)
before = digest('/sandbox/input/plate.FCStd')
shutil.copyfile('/sandbox/input/plate.FCStd', root/'plate-before.FCStd')
changed = execute(mutation([{'op_id': 'widen', 'action': 'sketch.set_constraint', 'args': {
    'sketch': 'BaseSketch', 'constraint_index': width, 'expected_type': 'Distance', 'value_mm': 80}}], 'wide'), 'plate.FCStd')
doc = App.openDocument(changed['files']['fcstd']); doc.recompute()
shape = Part.read(changed['files']['step'])
circles = [edge.Curve for edge in shape.Edges if isinstance(edge.Curve, Part.Circle) and abs(edge.Curve.Radius-3)<1e-7]
assert circles and abs(shape.BoundBox.XLength-80)<1e-7
center_x = circles[0].Center.x
centered = abs(center_x - (shape.BoundBox.XMin+shape.BoundBox.XMax)/2)<1e-7
assert digest('/sandbox/input/plate.FCStd') == before
evidence.append({'case': 'V01', 'centered_after_width_change': centered, 'hole_world_x': center_x,
    'expected_world_x': 53., 'before_sha256': before, 'after_sha256': digest(changed['files']['fcstd']),
    'width_constraint_index': width, 'hole_diameter_unchanged': True, 'saved_and_reopened': True})
App.closeDocument(doc.Name)

operations = []
def add(action, **args):
    operations.append({'op_id': 'tube-'+str(len(operations)), 'action': action, 'args': args})
add('sketch.create', name='Annulus')
for radius in (5,4):
    add('sketch.add_profile', sketch='Annulus', geometry={'kind': 'circle', 'center': {'x': 0, 'y': 0}, 'radius_mm': radius})
add('feature.pad', name='Tube', profile='Annulus', length_mm=10)
pipe = execute(mutation(operations, 'tube'))
doc = App.openDocument(pipe['files']['fcstd'])
indices = {int(c.Value): i for i,c in enumerate(doc.Annulus.Constraints) if c.Type=='Radius'}
doc.saveAs('/sandbox/input/tube.FCStd'); App.closeDocument(doc.Name)
before = digest('/sandbox/input/tube.FCStd')
shutil.copyfile('/sandbox/input/tube.FCStd', root/'tube-before.FCStd')
for order in ((4,5),(5,4)):
    ops = [{'op_id': 'radius-'+str(old), 'action': 'sketch.set_constraint', 'args': {
        'sketch': 'Annulus', 'constraint_index': indices[old], 'expected_type': 'Radius', 'value_mm': old+2}}
        for old in order]
    try:
        result = execute(mutation(ops, 'tube'+str(order[0])), 'tube.FCStd')
        shape = Part.read(result['files']['step'])
        assert shape.isValid() and len(shape.Solids)==1 and abs(shape.Volume - math.pi*(49-36)*10)<1e-5
        passed, error = True, None
    except runner.FreeCADRunnerError as exc:
        passed, error = False, {'code': exc.code, 'message': str(exc), 'op_id': exc.op_id}
    assert digest('/sandbox/input/tube.FCStd') == before
    evidence.append({'case': 'V02', 'order': list(order), 'passed': passed, 'error': error,
        'before_sha256': before, 'input_unchanged': True})

# Raw operation order remains explicit. Reject an unsafe shaft-first edit
# before FreeCAD silently replaces custom entrance dimensions. The structured
# batch compiler's input-order independence is also exercised through HTTP.
from freecad_state_projector import project_parameters
operations = []
for name, z, radius in [('Disc',0,15),('BoreProfile',12,2)]:
    add('sketch.create',name=name,frame={'origin':{'x':0,'y':0,'z':z},'normal':[0,0,1],'x_axis':[1,0,0]})
    add('sketch.add_profile',sketch=name,geometry={'kind':'circle','center':{'x':0,'y':0},'radius_mm':radius})
    if name=='Disc':
        add('feature.pad',name='DiscPad',profile=name,length_mm=12)
add('feature.hole',name='Bore',profile='BoreProfile',diameter_mm=4,through_all=True,
    cut={'kind':'counterbore','diameter_mm':8,'depth_mm':3})
bore=execute(mutation(operations,'counterbore'))
doc=App.openDocument(bore['files']['fcstd'])
editable={p['property_name'] for p in project_parameters(doc.Bore) if p['editable']}
assert {'Diameter','HoleCutDiameter'}<=editable
doc.saveAs('/sandbox/input/bore.FCStd'); App.closeDocument(doc.Name)
before=digest('/sandbox/input/bore.FCStd')
shutil.copyfile('/sandbox/input/bore.FCStd',root/'bore-before.FCStd')
for order in [('Diameter','HoleCutDiameter'),('HoleCutDiameter','Diameter')]:
    ops=[{'op_id':'joint-'+name.lower(),'action':'property.set','args':{
        'object':'Bore','property':name,'value':10 if name=='Diameter' else 12,
        'expected_property_type':'App::PropertyLength','unit':'mm'}} for name in order]
    try:
        result=execute(mutation(ops,'joint_'+order[0].lower()),'bore.FCStd')
    except runner.FreeCADRunnerError as exc:
        assert order[0]=='Diameter' and exc.code=='parameter_value_out_of_range'
        assert digest('/sandbox/input/bore.FCStd')==before
        evidence.append({'case':'V02-supported-property-set','order':list(order),'passed':True,
            'unsafe_raw_order_rejected':True,'diagnostic':exc.code,
            'before_sha256':before,'input_unchanged':True})
        continue
    assert order[0]=='HoleCutDiameter', 'unsafe raw order silently changed custom dimensions'
    doc=App.openDocument(result['files']['fcstd'])
    assert doc.Bore.Diameter.Value==10 and doc.Bore.HoleCutDiameter.Value==12
    assert doc.Bore.HoleCutDepth.Value==3
    shape=Part.read(result['files']['step'])
    assert shape.isValid() and len(shape.Solids)==1
    assert abs(shape.Volume-math.pi*(15**2*12-5**2*9-6**2*3))<1e-5
    App.closeDocument(doc.Name)
    assert digest('/sandbox/input/bore.FCStd')==before
    evidence.append({'case':'V02-supported-property-set','order':list(order),'passed':True,
        'before_sha256':before,'input_unchanged':True,'custom_depth_preserved':True,
        'final_step_volume_mm3':shape.Volume})

Path('/sandbox/output/dependency-evidence.json').write_text(json.dumps(evidence, indent=2))
if os.getenv('CAD_DEPENDENCY_EVIDENCE_DIR'):
    destination = Path(os.environ['CAD_DEPENDENCY_EVIDENCE_DIR'])
    destination.mkdir(parents=True, exist_ok=True)
    # Export artifacts without changing metadata on the host-owned mount root.
    for artifact in root.iterdir():
        target = destination / artifact.name
        if artifact.is_dir():
            shutil.copytree(artifact, target, dirs_exist_ok=True)
        else:
            shutil.copy2(artifact, target)
print('CAD_PARAMETER_DEPENDENCIES_CONTRACT=' + json.dumps(evidence))
if os.getenv('CAD_EXPECT_BASELINE_FAILURES') != '1':
    assert centered, 'V01: width changed but hole did not remain centered'
    assert all(row.get('passed', True) for row in evidence), 'V02: a legal final parameter batch was rejected'
