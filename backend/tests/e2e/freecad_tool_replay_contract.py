"""Independently replay the retained live 60×40×8 / Ø6 plate tool case.

Run inside the pinned FreeCAD runtime with CAD_TOOL_SOURCE_EVIDENCE pointing to
source recovered from the completed Temporal history, not a handwritten model.
Expected dimensions below belong to that fixed test's user requirement.
"""
import hashlib
import json
import math
import os
import shutil
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, '/opt/cad-agent')
import freecad_entry as runner

evidence = json.loads(Path(os.environ.get('CAD_TOOL_SOURCE_EVIDENCE',
    '/tests/fixtures/live_freecad_tool_plate_20260928.json')).read_text())
assert evidence['sources']
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    previous = None
    checkpoint_hashes = []
    for index, source in enumerate(evidence['sources']):
        assert hashlib.sha256(source['source_code'].encode()).hexdigest() == source['sha256']
        runner.INPUT_ROOT = root / f'input-{index}'; runner.INPUT_ROOT.mkdir()
        runner.OUTPUT_ROOT = root / f'output-{index}'; runner.OUTPUT_ROOT.mkdir()
        inputs = {}
        if previous:
            shutil.copyfile(previous, runner.INPUT_ROOT / 'base.FCStd')
            inputs['base'] = 'base.FCStd'
        plan = json.loads(source['source_code'])
        assert plan.get('execution_mode', 'final') == ('final' if index == len(evidence['sources'])-1 else 'checkpoint')
        result = runner.run_task({'schema_version': 'mcad-capability-task.v1',
            'capability': 'freecad', 'operation': 'execute', 'inputs': inputs,
            'params': {'plan': plan}})
        assert result['status'] == 'succeeded'
        previous = result['files']['fcstd']
        if plan.get('execution_mode') == 'checkpoint':
            assert 'step' not in result['files']
            checkpoint_hashes.append(hashlib.sha256(Path(previous).read_bytes()).hexdigest())
            intermediate = runner.App.openDocument(previous)
            try:
                bodies = [o for o in intermediate.Objects if o.TypeId == 'PartDesign::Body']
                assert len(bodies) == 1 and bodies[0].Tip.Shape.isValid()
                assert abs(bodies[0].Tip.Shape.Volume - 60*40*8) < 1e-6
            finally:
                runner.App.closeDocument(intermediate.Name)
    document = runner.App.openDocument(result['files']['fcstd'])
    try:
        bodies = [obj for obj in document.Objects if obj.TypeId == 'PartDesign::Body']
        assert len(bodies) == 1
        native = bodies[0].Tip.Shape
        step = runner.Part.read(result['files']['step'])
        for shape in (native, step):
            assert shape.isValid() and len(shape.Solids) == 1
            bounds = shape.BoundBox
            assert all(abs(a-b) < 1e-6 for a, b in zip(
                (bounds.XLength, bounds.YLength, bounds.ZLength), (60, 40, 8)))
            assert abs(shape.Volume - (60*40*8 - math.pi*3**2*8)) < 1e-5
            cylinders = [face for face in shape.Faces
                         if face.Surface.TypeId == 'Part::GeomCylinder']
            assert len(cylinders) == 1
            bore = cylinders[0]
            assert abs(bore.Surface.Radius - 3) < 1e-7
            assert abs(bore.Area / (2*math.pi*3) - 8) < 1e-6
            assert abs(bore.Surface.Center.x - (bounds.XMin+bounds.XMax)/2) < 1e-6
            assert abs(bore.Surface.Center.y - (bounds.YMin+bounds.YMax)/2) < 1e-6
        difference = native.cut(step).Volume + step.cut(native).Volume
        assert difference < 1e-6
    finally:
        runner.App.closeDocument(document.Name)
print(os.environ.get('CAD_TOOL_REPLAY_MARKER', 'CAD_TOOL_REPLAY_CONTRACT=') + json.dumps({'passed': True, 'source_sha256': source['sha256'],
    'executed_turns': len(evidence['sources']), 'checkpoint_sha256': checkpoint_hashes,
    'native_and_step': True, 'symmetric_difference_mm3': difference,
    'hole_count': 1, 'hole_diameter_mm': 6, 'hole_depth_mm': 8, 'centered': True}))
