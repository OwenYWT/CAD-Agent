"""Closed wire != valid face: independent native profile gate regression."""
import json
import sys
sys.path.insert(0, '/opt/cad-agent')
import FreeCAD as App
import Part
from freecad_constraint_validation import ConstraintVerificationError, validate_profile

checks = []
for crossed in (False, True):
    document = App.newDocument('ProfileGate')
    try:
        sketch = document.addObject('Sketcher::SketchObject', 'Section')
        points = [(0, 0), (5, 0), (5, -75), (-30, -75), (-30, -80), (0, -80)]
        if not crossed:
            points = [(x, -155-y if y in (-75, -80) else y) for x, y in points]
        for a, b in zip(points, points[1:] + points[:1]):
            sketch.addGeometry(Part.LineSegment(App.Vector(*a, 0), App.Vector(*b, 0)), False)
        document.recompute()
        assert sketch.Shape.isValid() and sketch.Shape.Wires[0].isClosed()
        try:
            validate_profile(sketch, closed=True)
        except ConstraintVerificationError:
            assert crossed
        else:
            assert not crossed, 'closed self-intersecting wire must never reach Pad'
        checks.append({'crossed': crossed, 'outcome': 'rejected' if crossed else 'passed'})
    finally:
        App.closeDocument(document.Name)
print('CAD_CONSTRAINT_PROFILE_CONTRACT=' + json.dumps(checks))
