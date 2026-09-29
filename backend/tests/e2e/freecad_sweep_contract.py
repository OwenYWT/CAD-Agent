"""Native curved hollow sweeps with analytic volume and live profile edits."""
import json
import math
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, '/opt/cad-agent')
import freecad_entry as runner
App, Part = runner.App, runner.Part

with tempfile.TemporaryDirectory() as root:
    root = Path(root)
    runner.INPUT_ROOT = root / 'input'; runner.INPUT_ROOT.mkdir()
    runner.OUTPUT_ROOT = root / 'output'; runner.OUTPUT_ROOT.mkdir()
    for radius, outer, inner, angle in [(45, 14, 11, 90), (30, 8, 6, 60)]:
        operations=[]
        def add(action, **args):
            operations.append({'op_id':f'op-{len(operations)}','action':action,'args':args})
        add('sketch.create', name='Path')
        add('sketch.add_profile', sketch='Path', geometry={'kind':'arc','center':{'x':0,'y':radius},
            'radius_mm':radius,'start_angle_deg':270,'end_angle_deg':270+angle})
        add('sketch.create', name='Section', frame={'origin':{'x':0,'y':0,'z':0},'normal':[1,0,0],'x_axis':[0,1,0]})
        for r in [outer,inner]:
            add('sketch.add_profile', sketch='Section', geometry={'kind':'circle','center':{'x':0,'y':0},'radius_mm':r})
        add('feature.sweep', name='Pipe', profile='Section', path='Path')
        straight=20
        a=math.radians(angle)
        ends=[('Inlet',(0,0,0),(-1,0,0)),
              ('Outlet',(radius*math.sin(a),radius*(1-math.cos(a)),0),(math.cos(a),math.sin(a),0))]
        for name,origin,normal in ends:
            add('sketch.create',name=name,frame={'origin':dict(zip('xyz',origin)),'normal':normal,'x_axis':[0,0,1]})
            for r in [outer,inner]:
                add('sketch.add_profile',sketch=name,geometry={'kind':'circle','center':{'x':0,'y':0},'radius_mm':r})
            add('feature.pad',name=name+'Pad',profile=name,length_mm=straight)
        add('document.export' , formats=['fcstd','step'], basename='sweep')
        result=runner.run_task({'schema_version':'mcad-capability-task.v1','capability':'freecad','operation':'execute',
            'inputs':{},'params':{'plan':{'schema_version':'freecad-operation-plan.v1','document_name':'Sweep','operations':operations}}})
        doc=App.openDocument(result['files']['fcstd'])
        assert doc.Pipe.TypeId=='PartDesign::AdditivePipe'
        s=Part.read(result['files']['step']);expected=math.pi*(outer**2-inner**2)*(radius*math.radians(angle)+2*straight)
        assert s.isValid() and len(s.Solids)==1 and abs(s.Volume-expected)<1e-4
        planes=[f for f in s.Faces if f.Surface.TypeId=='Part::GeomPlane']
        assert len(planes)==2 and all(abs(f.Area-math.pi*(outer**2-inner**2))<1e-5 for f in planes)
        index=next(i for i,c in enumerate(doc.Section.Constraints) if c.Type=='Radius' and abs(c.Value-outer)<1e-7)
        doc.Section.setDatum(index,App.Units.Quantity(f'{outer+1} mm'));doc.recompute()
        expected=math.pi*((outer+1)**2-inner**2)*radius*math.radians(angle)
        assert doc.Pipe.Shape.isValid() and abs(doc.Pipe.Shape.Volume-expected)<1e-4
        App.closeDocument(doc.Name)
print('CAD_SWEEP_CONTRACT='+json.dumps({'analytic_open_sweeps':2,'native_dependency_edits':2, 'straight_end_sections':4}))
