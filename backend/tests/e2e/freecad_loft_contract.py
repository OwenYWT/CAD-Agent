"""Open hollow transitions, measured on the final solid rather than the plan."""
import json
import math
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, '/opt/cad-agent')
import FreeCAD as App
import Part
import freecad_entry as runner

with tempfile.TemporaryDirectory() as root:
    root = Path(root)
    runner.INPUT_ROOT = root / 'input'; runner.INPUT_ROOT.mkdir()
    runner.OUTPUT_ROOT = root / 'output'; runner.OUTPUT_ROOT.mkdir()
    checked = []
    for bottom, top, height, wall in [(30, 20, 50, 2), (24, 16, 33, 3)]:
        operations = []
        def add(action, **args):
            operations.append({'op_id': f'op-{len(operations)}', 'action': action, 'args': args})
        # Two circular sections give an independent analytic frustum volume.
        for prefix, radii in [('Outer', [bottom, top]), ('Inner', [bottom-wall, top-wall])]:
            profiles = []
            for i, (z, r) in enumerate(zip([0, height], radii)):
                name = prefix + str(i); profiles.append(name)
                add('sketch.create', name=name, frame={'origin': {'x': 0, 'y': 0, 'z': z},
                    'normal': [0, 0, 1], 'x_axis': [1, 0, 0]})
                add('sketch.add_profile', sketch=name, geometry={'kind': 'circle',
                    'center': {'x': 0, 'y': 0}, 'radius_mm': r})
            add('feature.loft', name=prefix+'Loft', profiles=profiles, subtractive=prefix=='Inner', ruled=True)
        add('document.export', formats=['fcstd', 'step'], basename='loft')
        result = runner.run_task({'schema_version': 'mcad-capability-task.v1', 'capability': 'freecad',
            'operation': 'execute', 'inputs': {}, 'params': {'plan': {
                'schema_version': 'freecad-operation-plan.v1', 'document_name': 'Loft', 'operations': operations}}})
        doc = App.openDocument(result['files']['fcstd'])
        assert doc.OuterLoft.TypeId == 'PartDesign::AdditiveLoft'
        assert doc.InnerLoft.TypeId == 'PartDesign::SubtractiveLoft'
        s = Part.read(result['files']['step'])
        volume = lambda a,b: math.pi*height/3*(a*a+a*b+b*b)
        assert s.isValid() and len(s.Solids)==1
        assert abs(s.Volume-(volume(bottom,top)-volume(bottom-wall,top-wall)))<1e-4
        planes = [f for f in s.Faces if f.Surface.TypeId=='Part::GeomPlane']
        assert len(planes)==2
        for r in [bottom,top]:
            assert any(abs(f.Area-math.pi*(r*r-(r-wall)**2))<1e-5 for f in planes)
        # Native dependency editing must recompute the loft, not leave a frozen shape.
        doc.Outer1.setDatum(2, App.Units.Quantity(f'{top+1} mm')); doc.recompute()
        assert doc.InnerLoft.Shape.isValid() and doc.InnerLoft.Shape.Volume>s.Volume
        App.closeDocument(doc.Name)
        checked.append([bottom,top,height,wall])
print('CAD_LOFT_CONTRACT='+json.dumps({'analytic_open_transitions':checked,'native_dependency_edit':True}))
