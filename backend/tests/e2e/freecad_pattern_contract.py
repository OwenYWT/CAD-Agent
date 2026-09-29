"""Pattern actual removed material and preserve native editable feature counts."""
import json
import math
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, '/opt/cad-agent')
import freecad_entry as runner
from freecad_state_projector import project_parameters
App,Part=runner.App,runner.Part

with tempfile.TemporaryDirectory() as root:
    root=Path(root);runner.INPUT_ROOT=root/'input';runner.INPUT_ROOT.mkdir()
    runner.OUTPUT_ROOT=root/'output';runner.OUTPUT_ROOT.mkdir()
    for kind,count in [('polar',6),('linear',4)]:
        operations=[]
        def add(action,**args):
            operations.append({'op_id':f'op-{len(operations)}','action':action,'args':args})
        add('sketch.create',name='Base')
        add('sketch.add_profile',sketch='Base',geometry={'kind':'circle','center':{'x':0,'y':0},'radius_mm':50})
        add('feature.pad',name='Pad',profile='Base',length_mm=10)
        add('sketch.create',name='Profile',offset_mm=10)
        add('sketch.add_profile',sketch='Profile',geometry={'kind':'circle','center':{'x':25 if kind=='polar' else -20,'y':0},'radius_mm':2})
        add('feature.pocket',name='Hole',profile='Profile',through_all=True)
        args={'name':'Pattern','originals':['Hole'],'axis':'z' if kind=='polar' else 'x','occurrences':count}
        if kind=='polar':args['angle_deg']=360
        else:args['length_mm']=30
        add('feature.polar_pattern' if kind=='polar' else 'feature.linear_pattern',**args)
        add('document.export',formats=['fcstd','step'],basename='pattern')
        result=runner.run_task({'schema_version':'mcad-capability-task.v1','capability':'freecad','operation':'execute',
            'inputs':{},'params':{'plan':{'schema_version':'freecad-operation-plan.v1','document_name':'Pattern','operations':operations}}})
        doc=App.openDocument(result['files']['fcstd']);s=Part.read(result['files']['step'])
        assert s.isValid() and len(s.Solids)==1
        assert doc.Body.Tip == doc.Pattern
        assert abs(s.Volume-math.pi*(2500-count*4)*10)<1e-4, (kind, s.Volume)
        holes=[f for f in s.Faces if f.Surface.TypeId=='Part::GeomCylinder' and abs(f.Surface.Radius-2)<1e-7]
        assert len(holes)==count
        assert 'Occurrences' in {p['property_name'] for p in project_parameters(doc.Pattern)}
        runner._property_set(doc, {'object':'Pattern','property':'Occurrences','value':count+1,
            'expected_property_type':'App::PropertyIntegerConstraint','unit':None})
        doc.recompute()
        assert doc.Pattern.Shape.isValid() and abs(doc.Pattern.Shape.Volume-math.pi*(2500-(count+1)*4)*10)<1e-4
        doc.UndoMode=1
        prior=doc.Pattern.Occurrences
        doc.openTransaction('Invalid count')
        try:
            runner._property_set(doc, {'object':'Pattern','property':'Occurrences','value':0,
                'expected_property_type':'App::PropertyIntegerConstraint','unit':None})
        except runner.FreeCADRunnerError as exc:
            assert exc.code=='parameter_value_out_of_range'
        else:raise AssertionError('Kernel silently clamped an explicit count')
        finally:doc.abortTransaction()
        assert doc.Pattern.Occurrences==prior
        App.closeDocument(doc.Name)
print('CAD_PATTERN_CONTRACT='+json.dumps({'pattern_types':['polar','linear'],'counts_and_material_verified':True,'native_count_edits':2, 'clamped_values_rejected_and_rolled_back':2}))
