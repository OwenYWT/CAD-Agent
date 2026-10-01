"""Partial hollow torus checks prove native rotation and its open boundaries."""
import json
import math
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, '/opt/cad-agent')
import freecad_entry as runner
App,Part=runner.App,runner.Part

with tempfile.TemporaryDirectory() as root:
    root=Path(root);runner.INPUT_ROOT=root/'input';runner.INPUT_ROOT.mkdir()
    runner.OUTPUT_ROOT=root/'output';runner.OUTPUT_ROOT.mkdir()
    for angle in [90,240]:
        operations=[]
        def add(action,**args):
            operations.append({'op_id':f'op-{len(operations)}','action':action,'args':args})
        add('sketch.create',name='Section',frame={'origin':{'x':0,'y':0,'z':0},'normal':[0,-1,0],'x_axis':[1,0,0]})
        for r in [4,2]:add('sketch.add_profile',sketch='Section',geometry={'kind':'circle','center':{'x':40,'y':0},'radius_mm':r})
        add('feature.revolve',name='Ring',profile='Section',axis='z',angle_deg=angle)
        add('document.export',formats=['fcstd','step'],basename='revolve')
        result=runner.run_task({'schema_version':'mcad-capability-task.v1','capability':'freecad','operation':'execute',
            'inputs':{},'params':{'plan':{'schema_version':'freecad-operation-plan.v1','document_name':'Revolve','operations':operations}}})
        doc=App.openDocument(result['files']['fcstd']);s=Part.read(result['files']['step'])
        assert doc.Ring.TypeId=='PartDesign::Revolution'
        assert s.isValid() and len(s.Solids)==1 and abs(s.Volume-math.pi*12*40*math.radians(angle))<1e-4
        doc.Ring.Angle=angle+15;doc.recompute()
        assert doc.Ring.Shape.isValid() and abs(doc.Ring.Shape.Volume-math.pi*12*40*math.radians(angle+15))<1e-4
        App.closeDocument(doc.Name)
print('CAD_REVOLVE_CONTRACT='+json.dumps({'angles':[90,240],'analytic_volume_and_angle_edits':True}))
