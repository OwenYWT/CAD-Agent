"""Actual persisted topology identity across independent edits and translation."""
import json
import sys
from pathlib import Path

sys.path.insert(0, '/projector')
import FreeCAD as App
import Part
import Sketcher
from state_projector import project_saved_document

doc = App.newDocument('CheckpointAcceptance')
for suffix, x in [('A', 5), ('B', 25)]:
    body = doc.addObject('PartDesign::Body', 'Body' + suffix)
    sketch = body.newObject('Sketcher::SketchObject', 'Sketch' + suffix)
    sketch.addGeometry(Part.Circle(App.Vector(x, 5, 0), App.Vector(0, 0, 1), 5), False)
    for c in [Sketcher.Constraint('DistanceX', 0, 3, x),
              Sketcher.Constraint('DistanceY', 0, 3, 5), Sketcher.Constraint('Radius', 0, 5)]:
        sketch.addConstraint(c)
    pad = body.newObject('PartDesign::Pad', 'Pad' + suffix)
    pad.Profile = sketch
    pad.Length = 10
doc.recompute()
path = Path('/tmp/checkpoint-acceptance.FCStd')
doc.saveAs(str(path))
before = {o['name']: o for o in project_saved_document(doc, path)['objects']}
App.closeDocument(doc.Name)
doc = App.openDocument(str(path))
doc.PadB.Length = 13
doc.recompute()
doc.saveAs(str(path))
after = {o['name']: o for o in project_saved_document(doc, path)['objects']}
for name in ['SketchA', 'PadA']:
    assert before[name]['geometry_sha256'] == after[name]['geometry_sha256'], name
    assert before[name]['geometry_fingerprint_kind'] == 'fcstd-brep.v1'
assert before['PadB']['geometry_sha256'] != after['PadB']['geometry_sha256']
volume = after['PadA']['shape']['volume']
doc.BodyA.Placement.Base.x += 10
doc.recompute()
doc.saveAs(str(path))
moved = {o['name']: o for o in project_saved_document(doc, path)['objects']}
# Moving a body preserves the child's local shape. Its world-space body
# fingerprint changes; dependency closure also checks placement properties.
assert after['BodyA']['geometry_sha256'] != moved['BodyA']['geometry_sha256']
assert abs(moved['BodyA']['shape']['volume'] - volume) < 1e-6
print('CAD_CHECKPOINT_ACCEPTANCE=' + json.dumps({'independent_edit': True, 'translation_detected': True}), flush=True)
