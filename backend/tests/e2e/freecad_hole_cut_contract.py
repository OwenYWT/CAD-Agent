"""Verify final native/STEP hole form, depth and protected opposite entrance."""
import json
import math
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, '/opt/cad-agent')
import FreeCAD as App
import Part
import freecad_entry as runner
from freecad_state_projector import project_parameters

with tempfile.TemporaryDirectory() as root:
    root = Path(root)
    runner.INPUT_ROOT = root / 'input'; runner.INPUT_ROOT.mkdir()
    runner.OUTPUT_ROOT = root / 'output'; runner.OUTPUT_ROOT.mkdir()
    checked = []
    for kind in ['counterbore', 'countersink']:
        for normal, x_axis in [([0, 0, 1], [1, 0, 0]), ([0, -1, 0], [1, 0, 0])]:
            operations = []
            def add(action, **args):
                operations.append({'op_id': f'op-{len(operations)}', 'action': action, 'args': args})
            for name, distance, radius in [('Base', 0, 15), ('Profile', 12, 2)]:
                add('sketch.create', name=name, frame={'origin': dict(zip('xyz', [distance*v for v in normal])),
                    'normal': normal, 'x_axis': x_axis})
                add('sketch.add_profile', sketch=name, geometry={'kind': 'circle',
                    'center': {'x': 0, 'y': 0}, 'radius_mm': radius})
                if name == 'Base': add('feature.pad', name='Pad', profile=name, length_mm=12)
            cut = {'kind': kind, 'diameter_mm': 8}
            if kind == 'counterbore':
                cut['depth_mm'] = 3
                removed = math.pi * (2**2 * 9 + 4**2 * 3)
                straight_length = 9
            else:
                cut['angle_deg'] = 82
                h = 2 / math.tan(math.radians(41))
                removed = math.pi * 2**2 * (12 - h) + math.pi*h/3*(4**2 + 4*2 + 2**2)
                straight_length = 12 - h
            add('feature.hole', name='Hole', profile='Profile', diameter_mm=4, through_all=True, cut=cut)
            add('document.export', formats=['fcstd', 'step'], basename='hole')
            result = runner.run_task({'schema_version': 'mcad-capability-task.v1', 'capability': 'freecad',
                'operation': 'execute', 'inputs': {}, 'params': {'plan': {
                    'schema_version': 'freecad-operation-plan.v1', 'document_name': 'HoleTest', 'operations': operations}}})
            doc = App.openDocument(result['files']['fcstd'])
            assert doc.Hole.HoleCutType.lower() == kind
            parameters = {p['property_name'] for p in project_parameters(doc.Hole)}
            assert {'Diameter', 'HoleCutDiameter'} <= parameters, parameters
            assert ('HoleCutDepth' if kind == 'counterbore' else 'HoleCutCountersinkAngle') in parameters
            shape = Part.read(result['files']['step'])
            assert shape.isValid() and len(shape.Solids) == 1
            assert abs(shape.Volume - (math.pi * 15**2 * 12 - removed)) < 1e-5
            shaft = [f for f in shape.Faces if f.Surface.TypeId == 'Part::GeomCylinder' and abs(f.Surface.Radius-2)<1e-7]
            assert len(shaft) == 1 and abs(shaft[0].Area/(4*math.pi)-straight_length)<1e-6
            # Protected opposite entrance remains a flat ring with only the shaft opening.
            assert any(f.Surface.TypeId == 'Part::GeomPlane' and abs(f.Area-math.pi*(15**2-2**2))<1e-5 for f in shape.Faces)
            runner._property_set(doc,{'object':'Hole','property':'HoleCutDiameter','value':9,
                'expected_property_type':'App::PropertyLength','unit':'mm'})
            runner._validate_document(doc,op_id='head-edit',action='property.set')
            edited=runner._export_document(doc,{'formats':['fcstd','step'],'basename':'edited'})
            updated=Part.read(edited['step'])
            head_radius=4.5
            depth=3 if kind=='counterbore' else (head_radius-2)/math.tan(math.radians(41))
            removed=(math.pi*(4*(12-depth)+head_radius**2*depth) if kind=='counterbore'
                else math.pi*4*(12-depth)+math.pi*depth/3*(head_radius**2+head_radius*2+4))
            assert abs(updated.Volume-(math.pi*225*12-removed))<1e-5
            assert any(f.Surface.TypeId=='Part::GeomPlane' and abs(f.Area-math.pi*(225-4))<1e-5 for f in updated.Faces)
            doc.UndoMode=1;doc.openTransaction('Invalid entrance')
            try:
                runner._property_set(doc,{'object':'Hole','property':'HoleCutDiameter','value':3,
                    'expected_property_type':'App::PropertyLength','unit':'mm'})
                runner._validate_document(doc,op_id='invalid-head-edit',action='property.set')
            except runner.FreeCADRunnerError:
                pass
            else:
                raise AssertionError('Hole mouth smaller than shaft accepted')
            finally:doc.abortTransaction()
            assert abs(doc.Hole.HoleCutDiameter.Value-9)<1e-7
            App.closeDocument(doc.Name)
            checked.append(kind)
print('CAD_HOLE_CUT_CONTRACT=' + json.dumps({'final_step_forms_verified': checked, 'opposite_entrances_preserved': 4, 'head_diameter_edits':4, 'invalid_edits_rejected':4}))
