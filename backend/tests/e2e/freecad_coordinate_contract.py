"""Real placement probe, independent of compiler transformation helpers."""
import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, '/opt/cad-agent')
import FreeCAD as App
import Part
import freecad_entry as runner


def near(actual, expected):
    assert (actual - App.Vector(*expected)).Length < 1e-7, (actual, expected)


document = App.newDocument('CoordinateContract')
try:
    # Retained v1 plans must not silently move during a source replay.
    legacy = [('xy', (0, 0, 10)), ('xz', (0, 0, 10)), ('yz', (0, 10, 0))]
    for plane, expected in legacy:
        runner._sketch_create(document, {'name': 'Old_' + plane, 'plane': plane, 'offset_mm': 10})
        document.recompute()
        near(document.getObject('Old_' + plane).Placement.Base, expected)

    body = document.getObject('Body')
    body.Placement = App.Placement(App.Vector(7, -3, 11), App.Rotation(App.Vector(1, 1, 1), 37))
    frames = [('xy', (0, 0, 1), (1, 0, 0)),
              ('xz', (0, -1, 0), (1, 0, 0)),
              ('yz', (1, 0, 0), (0, 1, 0))]
    count = 0
    for label, normal, x_axis in frames:
        for offset in [-13, 0, 17]:
            origin = tuple(offset * v for v in normal)
            name = f'New_{label}_{count}'
            runner._sketch_create(document, {'name': name, 'frame': {
                'origin': dict(zip(('x', 'y', 'z'), origin)),
                'normal': normal, 'x_axis': x_axis}})
            document.recompute()
            sketch = document.getObject(name)
            near(sketch.Placement.Base, origin)
            near(sketch.Placement.Rotation.multVec(App.Vector(0, 0, 1)), normal)
            expected_global = body.Placement.multVec(App.Vector(*origin))
            assert (sketch.getGlobalPlacement().Base - expected_global).Length < 1e-7
            count += 1
finally:
    App.closeDocument(document.Name)

# Complete operations/export/reopen path, with signed and zero sketch coordinates.
with tempfile.TemporaryDirectory() as root:
    root = Path(root)
    runner.INPUT_ROOT = root / 'input'; runner.INPUT_ROOT.mkdir()
    runner.OUTPUT_ROOT = root / 'output'; runner.OUTPUT_ROOT.mkdir()
    for label, normal, x_axis in frames:
        for offset in [-7, 9]:
            operations = []
            def add(action, **args):
                operations.append({'op_id': f'op-{len(operations)}', 'action': action, 'args': args})
            for name, radius, distance in [('Base', 10, offset), ('Cut', 3, offset + 20)]:
                origin = dict(zip(('x', 'y', 'z'), (distance * v for v in normal)))
                add('sketch.create', name=name, frame={'origin': origin, 'normal': normal, 'x_axis': x_axis})
                add('sketch.add_geometry', sketch=name,
                    geometry={'kind': 'circle', 'center': {'x': 0, 'y': -2}, 'radius_mm': radius})
                for kind, position, value in [('distance_x', 3, 0), ('distance_y', 3, -2), ('radius', None, radius)]:
                    add('sketch.add_constraint', sketch=name, kind=kind,
                        first={'geometry_index': 0, 'point_position': position}, value_mm=value)
                if name == 'Base':
                    add('feature.pad', name='Solid', profile=name, length_mm=20)
                else:
                    add('feature.pocket', name='BlindHole', profile=name, length_mm=5)
            add('document.export', formats=['fcstd', 'step', 'stl'], basename='probe')
            result = runner.run_task({'schema_version': 'mcad-capability-task.v1', 'capability': 'freecad',
                'operation': 'execute', 'inputs': {}, 'params': {'plan': {
                    'schema_version': 'freecad-operation-plan.v1', 'document_name': 'Probe', 'operations': operations}}})
            expected = math.pi * (10**2 * 20 - 3**2 * 5)
            restored = App.openDocument(result['files']['fcstd'])
            assert restored.Body.Tip.Shape.isValid()
            assert abs(restored.Body.Tip.Shape.Volume - expected) < 1e-5
            step = Part.read(result['files']['step'])
            assert step.isValid() and len(step.Solids) == 1
            assert abs(step.Volume - expected) < 1e-5
            holes = [f for f in step.Faces if f.Surface.TypeId == 'Part::GeomCylinder' and abs(f.Surface.Radius - 3) < 1e-7]
            assert len(holes) == 1 and abs(holes[0].Area / (6 * math.pi) - 5) < 1e-6
            App.closeDocument(restored.Name)
# A through cut anchored at a pad end must follow thickness edits in every frame.
with tempfile.TemporaryDirectory() as root:
    root=Path(root); runner.INPUT_ROOT=root/'input';runner.INPUT_ROOT.mkdir()
    runner.OUTPUT_ROOT=root/'output';runner.OUTPUT_ROOT.mkdir()
    for label,normal,x_axis in frames:
        operations=[]
        def add(action,**args):
            operations.append({'op_id':f'op-{len(operations)}','action':action,'args':args})
        for name,distance,radius in [('Base',-7,10),('Cut',13,3)]:
            add('sketch.create',name=name,frame={'origin':dict(zip('xyz',[distance*v for v in normal])),
                'normal':normal,'x_axis':x_axis})
            add('sketch.add_profile',sketch=name,geometry={'kind':'circle','center':{'x':0,'y':0},'radius_mm':radius})
            if name=='Base':add('feature.pad',name='Pad',profile=name,length_mm=20)
        add('feature.pocket',name='Hole',profile='Cut',through_all=True)
        add('document.export',formats=['fcstd','step'],basename='through')
        result=runner.run_task({'schema_version':'mcad-capability-task.v1','capability':'freecad','operation':'execute',
            'inputs':{},'params':{'plan':{'schema_version':'freecad-operation-plan.v1','document_name':'Through','operations':operations}}})
        doc=App.openDocument(result['files']['fcstd'])
        runner._property_set(doc,{'object':'Pad','property':'Length','value':30,
            'expected_property_type':'App::PropertyLength','unit':'mm'})
        doc.recompute()
        near(doc.Cut.Placement.Base, [23*v for v in normal])
        assert abs(doc.Body.Shape.Volume-math.pi*(100-9)*30)<1e-5
        App.closeDocument(doc.Name)
print('CAD_COORDINATE_CONTRACT=' + json.dumps({'explicit_frames': count, 'legacy_preserved': 3,
    'body_transform_verified': True, 'signed_coordinates_cut_export_reopen': 6, 'through_cut_thickness_edits':3}))
