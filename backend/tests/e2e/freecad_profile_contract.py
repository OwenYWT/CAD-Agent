"""Deterministic profiles keep geometry and dimensional constraints identical."""
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
    for x, y in [(0, 0), (-8, -12), (17, 31)]:
        operations = []
        def add(action, **args):
            operations.append({'op_id': f'op-{len(operations)}', 'action': action, 'args': args})
        add('sketch.create', name='Base')
        add('sketch.add_profile', sketch='Base', geometry={'kind': 'rectangle',
            'corner': {'x': x, 'y': y}, 'width_mm': 30, 'height_mm': 20})
        add('feature.pad', name='Pad', profile='Base', length_mm=4)
        add('sketch.create', name='Holes', offset_mm=4)
        for dx, dy, r in [(8, 7, 2), (22, 13, 3)]:
            add('sketch.add_profile', sketch='Holes', geometry={'kind': 'circle',
                'center': {'x': x + dx, 'y': y + dy}, 'radius_mm': r})
        add('feature.pocket', name='Through', profile='Holes', through_all=True)
        add('document.export', formats=['fcstd', 'step'], basename='profiles')
        result = runner.run_task({'schema_version': 'mcad-capability-task.v1', 'capability': 'freecad',
            'operation': 'execute', 'inputs': {}, 'params': {'plan': {
                'schema_version': 'freecad-operation-plan.v1', 'document_name': 'Profiles', 'operations': operations}}})
        doc = App.openDocument(result['files']['fcstd'])
        assert [o.Name for o in doc.Objects if o.TypeId == 'PartDesign::Body'] == ['Body']
        assert doc.Base.getParentGeoFeatureGroup() == doc.Body == doc.Holes.getParentGeoFeatureGroup()
        assert doc.Base.FullyConstrained and doc.Holes.FullyConstrained
        assert doc.Holes.GeometryCount == 2 and doc.Holes.ConstraintCount == 6
        expected = (600 - 13 * math.pi) * 4
        shape = Part.read(result['files']['step'])
        assert shape.isValid() and abs(shape.Volume - expected) < 1e-5
        assert abs(shape.BoundBox.XMin - x) < 1e-6 and abs(shape.BoundBox.YMin - y) < 1e-6
        App.closeDocument(doc.Name)
print('CAD_PROFILE_CONTRACT=' + json.dumps({'translations': 3, 'profiles_solved': 9, 'exact_volumes': 3}))
