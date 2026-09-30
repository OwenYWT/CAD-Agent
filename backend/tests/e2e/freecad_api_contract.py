"""Run actual previously unwrapped APIs, reopen/export, and reject API failure."""
import json
import math
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, '/opt/cad-agent')
import freecad_entry as runner
App=runner.App

def task(source, environment='headless', inputs=None, objects=('Box',)):
    return {'schema_version':'mcad-capability-task.v1','capability':'freecad','operation':'execute',
        'inputs':inputs or {},'params':{'plan':{'schema_version':'freecad-operation-plan.v1',
        'document_name':'APIModel','operations':[
            {'op_id':'api','action':'api.execute','args':{'source':source,'environment':environment,'modules':['Part','Spreadsheet']}},
            {'op_id':'export','action':'document.export','args':{'formats':['fcstd','step'],'basename':'model','objects':list(objects)}}]}}}

with tempfile.TemporaryDirectory() as root:
    root=Path(root);runner.OUTPUT_ROOT=root/'output';runner.OUTPUT_ROOT.mkdir()
    runner.INPUT_ROOT=root/'input';runner.INPUT_ROOT.mkdir()
    for size in [7,13]:
        source=f'''sheet=document.addObject('Spreadsheet::Sheet','Dimensions')
sheet.set('A1','{size} mm')
sheet.setAlias('A1','Side')
box=document.addObject('Part::Box','Box')
box.setExpression('Length','Dimensions.Side')
box.Width={size+2}
box.Height={size+4}
'''
        result=runner.run_task(task(source))
        assert result['status']=='succeeded'
        shape=runner.Part.read(result['files']['step'])
        assert len(shape.Solids)==1 and abs(shape.Volume-size*(size+2)*(size+4))<1e-6
        d=App.openDocument(result['files']['fcstd'])
        d.Dimensions.set('A1',f'{size+1} mm');d.recompute()
        assert abs(d.Box.Shape.Volume-(size+1)*(size+2)*(size+4))<1e-6
        App.closeDocument(d.Name)
    boolean=runner.run_task(task("""base=document.addObject('Part::Box','Base')
base.Length=40;base.Width=30;base.Height=8
tool=document.addObject('Part::Cylinder','Tool');tool.Radius=3;tool.Height=8
tool.Placement.Base=App.Vector(20,15,0)
cut=document.addObject('Part::Cut','Final');cut.Base=base;cut.Tool=tool
""",objects=('Final',)))
    final_shape=runner.Part.read(boolean['files']['step'])
    assert len(final_shape.Solids)==1
    assert abs(final_shape.Volume-(40*30*8-math.pi*3**2*8))<1e-6
    document=App.openDocument(boolean['files']['fcstd'])
    from freecad_scene import component_shapes
    assert [obj.Name for obj,shape in component_shapes(document)]==['Final']
    assert document.getObject('Base') and document.getObject('Tool')
    App.closeDocument(document.Name)
    # Delivery selection does not disable exact BRep engineering measurements.
    internal_task=task("box=document.addObject('Part::Box','Box');box.Length=20;box.Width=20;box.Height=20")
    internal_task['params']['plan']['operations'][-1]['args']['formats']=['fcstd','stl']
    internal_task['params']['measurement_formats']=['step']
    internal=runner.run_task(internal_task)
    assert 'step' not in internal['files'] and 'stl' in internal['files']
    assert abs(runner.Part.read(internal['files']['verification_step']).Volume-8000)<1e-6
    internal_task['params']['plan']['execution_mode']='checkpoint'
    try:
        runner.run_task(internal_task)
    except runner.FreeCADRunnerError as exc:
        assert exc.code=='invalid_task'
    else:
        raise AssertionError('checkpoint accepted final measurement export')
    gui=runner.run_task(task("""Gui.activateWorkbench('PartWorkbench')
box=document.addObject('Part::Box','GuiBox')
box.Length=17;box.Width=9;box.Height=5
document.recompute()
Gui.activeDocument().activeView().viewAxonometric()
Gui.activeDocument().activeView().fitAll()
""",environment='gui',objects=('GuiBox',)))
    assert abs(runner.Part.read(gui['files']['step']).Volume-17*9*5)<1e-6
    before=Path(gui['files']['fcstd']).read_bytes()
    for source,code in [("raise ValueError('intentional API failure')",'api_execution_failed'), ("import missing_freecad_plugin_for_contract",'api_dependency_unavailable')]:
        try:
            runner.run_task(task(source))
        except runner.FreeCADRunnerError as exc:
            assert exc.code==code,exc
        else:
            raise AssertionError('failed program authorized a candidate')
        assert Path(gui['files']['fcstd']).read_bytes()==before
print('CAD_API_CONTRACT='+json.dumps({'spreadsheet_expression_variants':2,'gui_workbench':True,'failed_programs_rejected':2}))
