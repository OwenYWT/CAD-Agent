"""Real native perturbations include downstream editable feature parameters."""
import json
import sys
sys.path.insert(0, '/opt/cad-agent')
import FreeCAD as App
import Part
import Sketcher
import freecad_entry as runner
from freecad_failure_snapshot import sketch_snapshot, geometry_snapshot
from freecad_constraint_relationships import native_args
from freecad_constraint_validation import verify_document, downstream_parameters

document = App.newDocument('ParameterProof')
try:
    body = document.addObject('PartDesign::Body', 'Body')
    sketch = body.newObject('Sketcher::SketchObject', 'Section')
    sketch.addGeometry(Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 10), False)
    sketch.addConstraint(Sketcher.Constraint('Coincident', 0, 3, -1, 1))
    sketch.addConstraint(Sketcher.Constraint('Radius', 0, 10.0))
    pad = body.newObject('PartDesign::Pad', 'Pad'); pad.Profile = sketch; pad.Length = 8
    document.recompute()
    circular_edge = next(f'Edge{i}' for i, edge in enumerate(pad.Shape.Edges, 1)
                         if isinstance(edge.Curve, Part.Circle))
    fillet = body.newObject('PartDesign::Fillet', 'Round'); fillet.Base = (pad, [circular_edge]); fillet.Radius = 1
    document.recompute()
    assert fillet.Shape.isValid() and len(fillet.Shape.Solids) == 1
    inventory = downstream_parameters(document, {'Section'})
    assert {(p['object'], p['property']) for p in inventory} == {('Pad', 'Length'), ('Round', 'Radius')}
    specification = {'construction_geometry': geometry_snapshot(sketch), 'derivations': [],
        'constraints': [{'logical_id': row['logical_id'], 'native_index': row['index'],
                         'args': {**native_args(row), 'driving': True}}
                        for row in sketch_snapshot(sketch, None)['constraints']]}
    before = body.Shape.Volume
    report = verify_document(document, {'sketches': {'Section': specification}}, runner._validate_document)
    assert len(report['parameter_probes']) == 6, report['parameter_probes']
    assert {(p['object'], p.get('property')) for p in report['parameter_probes']} == {
        ('Section', None), ('Pad', 'Length'), ('Round', 'Radius')}
    assert body.Shape.Volume == before and fillet.Radius.Value == 1 and pad.Length.Value == 8
    # Expression-controlled properties are edited at their source, never by
    # silently clearing the expression to force a probe to succeed.
    fillet.setExpression('Radius', 'Pad.Length / 8')
    document.recompute()
    assert {(p['object'], p['property']) for p in downstream_parameters(document, {'Section'})} == {('Pad', 'Length')}
    report = verify_document(document, {'sketches': {'Section': specification}}, runner._validate_document)
    assert len(report['parameter_probes']) == 4 and fillet.ExpressionEngine
finally:
    App.closeDocument(document.Name)

document = App.newDocument('RevolutionProof')
try:
    runner._sketch_create(document, {'name': 'Profile', 'frame': {
        'origin': {'x': 0, 'y': 0, 'z': 0}, 'normal': [0, -1, 0], 'x_axis': [1, 0, 0]}})
    runner._sketch_add_profile(document, {'sketch': 'Profile', 'geometry': {'kind': 'rectangle',
        'corner': {'x': 0, 'y': 0}, 'width_mm': 12, 'height_mm': 17}})
    document.recompute()
    runner._feature_revolve(document, {'name': 'Turn', 'profile': 'Profile', 'axis': 'z', 'angle_deg': 360})
    document.Body.Placement = App.Placement(App.Vector(7, -4, 9), App.Rotation(App.Vector(1, 1, 0), 45))
    document.recompute()
    sketch = document.Profile
    specification = {'construction_geometry': geometry_snapshot(sketch), 'derivations': [], 'constraints': [
        {'logical_id': row['logical_id'], 'native_index': row['index'], 'args': {**native_args(row), 'driving': True}}
        for row in sketch_snapshot(sketch, None)['constraints']]}
    report = verify_document(document, {'sketches': {'Profile': specification}}, runner._validate_document)
    probes = report['parameter_probes']
    assert len(report['axis_anchors']) == 1 and report['axis_anchors'][0]['revolution'] == 'Turn'
    assert len(probes) == 8
    # The zero Y coordinate locates the section along the axis and is still independently editable.
    assert {p['probe_value'] > 0 for p in probes if p['object'] == 'Profile' and p['value'] == 0} == {False, True}
    angles = {p['probe_value'] for p in probes if p.get('property') == 'Angle'}
    assert len(angles) == 2 and all(0 < value < 360 for value in angles)
    assert document.Turn.Angle.Value == 360 and document.Body.Shape.isValid()
finally:
    App.closeDocument(document.Name)
print('CAD_CONSTRAINT_PARAMETER_CONTRACT=' + json.dumps({'independent_probes': 6,
    'expression_source_probes': 4, 'revolution_probes': len(probes), 'angle_edits': sorted(angles)}))
