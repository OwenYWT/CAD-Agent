"""Actual FreeCAD dimensional solve, signed coordinates and failure isolation."""
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile

sys.path.insert(0,'/opt/cad-agent')
import FreeCAD as App
import Part
import Sketcher
import freecad_entry as runner


with tempfile.TemporaryDirectory() as root:
    root=Path(root);inputs=root/'input';outputs=root/'output';inputs.mkdir();outputs.mkdir()
    runner.INPUT_ROOT=inputs;runner.OUTPUT_ROOT=outputs
    doc=App.newDocument('SketchAcceptance');body=doc.addObject('PartDesign::Body','Body')
    sketch=doc.addObject('Sketcher::SketchObject','Sketch');body.addObject(sketch)
    sketch.addGeometry(Part.Circle(App.Vector(5,5,0),App.Vector(0,0,1),5),False)
    sketch.addConstraint(Sketcher.Constraint('DistanceX',0,3,5.0))
    sketch.addConstraint(Sketcher.Constraint('DistanceY',0,3,5.0))
    sketch.addConstraint(Sketcher.Constraint('Radius',0,5.0))
    pad=body.newObject('PartDesign::Pad','Pad');pad.Profile=sketch;pad.Length=10
    doc.recompute();assert sketch.FullyConstrained and sketch.solve()==0
    base=inputs/'base.FCStd';doc.saveAs(str(base));App.closeDocument(doc.Name)
    digest=hashlib.sha256(base.read_bytes()).hexdigest()
    def task(edits):
        return {'schema_version':'mcad-capability-task.v1','capability':'freecad','operation':'execute',
            'params':{'plan':{'schema_version':'freecad-operation-plan.v1','document_name':'SketchAcceptance',
                'operations':[{'op_id':f'edit-{i}','action':'sketch.set_constraint','args':args} for i,args in enumerate(edits)]
                +[{'op_id':'export','action':'document.export','args':{'formats':['fcstd','step','stl'],'basename':'sketch_result'}}]}},
            'inputs':{'base':'base.FCStd'}}
    def edit(index,kind,value):
        return {'sketch':'Sketch','constraint_index':index,'expected_type':kind,'value_mm':value}
    result=runner.run_task(task([edit(2,'Radius',7),edit(0,'DistanceX',0),edit(1,'DistanceY',-3)]))
    assert result['status']=='succeeded',result
    state=json.loads(Path(result['files']['state']).read_text())
    sketch_state=next(o for o in state['objects'] if o['name']=='Sketch')
    constraints=sketch_state['inspection']['constraints']['items']
    assert [c['value'] for c in constraints]==[0,-3,7]
    assert all(c['driving'] for c in constraints) and sketch_state['sketch']['fully_constrained']
    restored=App.openDocument(result['files']['fcstd'])
    assert abs(restored.Pad.Shape.Volume-math.pi*49*10)<1e-7
    assert abs(restored.Sketch.Geometry[0].Center.y+3)<1e-9
    App.closeDocument(restored.Name)
    assert hashlib.sha256(base.read_bytes()).hexdigest()==digest
    for args,code in [(edit(2,'Diameter',7),'sketch_constraint_type_mismatch'),
                      (edit(2,'Radius',-1),'invalid_number'),(edit(999,'Radius',7),'sketch_constraint_missing')]:
        try:
            runner.run_task(task([args]))
        except runner.FreeCADRunnerError as exc:
            assert exc.code==code,(exc.code,str(exc))
        else:
            raise AssertionError('invalid constraint edit unexpectedly succeeded')
        assert hashlib.sha256(base.read_bytes()).hexdigest()==digest
    doc=App.openDocument(str(base));doc.Sketch.setDriving(2,False);doc.recompute();doc.save();App.closeDocument(doc.Name)
    try:
        runner.run_task(task([edit(2,'Radius',7)]))
    except runner.FreeCADRunnerError as exc:
        assert exc.code=='sketch_reference_constraint',(exc.code,str(exc))
    else:
        raise AssertionError('reference dimension unexpectedly editable')
    doc=App.newDocument('TriangleAcceptance');body=doc.addObject('PartDesign::Body','Body')
    sketch=doc.addObject('Sketcher::SketchObject','Sketch');body.addObject(sketch)
    points=[App.Vector(5,5,0),App.Vector(8,5,0),App.Vector(8,9,0)]
    for i in range(3):
        sketch.addGeometry(Part.LineSegment(points[i],points[(i+1)%3]),False)
    for i in range(3):
        sketch.addConstraint(Sketcher.Constraint('Coincident',i,2,(i+1)%3,1))
    sketch.addConstraint(Sketcher.Constraint('Horizontal',0))
    sketch.addConstraint(Sketcher.Constraint('DistanceX',0,1,5.0))
    sketch.addConstraint(Sketcher.Constraint('DistanceY',0,1,5.0))
    side=sketch.addConstraint(Sketcher.Constraint('Distance',0,3.0))
    sketch.addConstraint(Sketcher.Constraint('Distance',1,4.0))
    sketch.addConstraint(Sketcher.Constraint('Distance',2,5.0))
    doc.recompute();assert sketch.solve()==0 and sketch.FullyConstrained
    pad=body.newObject('PartDesign::Pad','Pad');pad.Profile=sketch;pad.Length=10
    doc.recompute();assert abs(pad.Shape.Volume-60)<1e-8
    doc.saveAs(str(base));App.closeDocument(doc.Name);digest=hashlib.sha256(base.read_bytes()).hexdigest()
    # Non-dimensional Coincident/Horizontal constraints cannot be queried via
    # getDriving. Their presence must not break a valid checkpoint export.
    assert runner.run_task(task([]))['status']=='succeeded'
    try:
        runner.run_task(task([edit(side,'Distance',10)]))
    except runner.FreeCADRunnerError as exc:
        assert exc.code in {'sketch_constraint_update_failed','sketch_solver_failed','sketch_conflicting_constraints','invalid_document_object'},(exc.code,str(exc))
    else:
        raise AssertionError('triangle inequality violation unexpectedly succeeded')
    assert hashlib.sha256(base.read_bytes()).hexdigest()==digest
    print('CAD_SKETCH_ACCEPTANCE='+json.dumps({'radius_and_signed_coordinates_solved':True,
        'native_solid_volume_verified':True,'reference_and_type_and_range_rejected':True,
        'impossible_triangle_rejected':True,'input_checkpoint_unchanged':True}),flush=True)
