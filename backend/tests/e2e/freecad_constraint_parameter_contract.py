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
    print('CAD_CONSTRAINT_PARAMETER_CONTRACT=' + json.dumps({'independent_probes': 6,
        'expression_source_probes': 4, 'original_unchanged': True, 'downstream_fillet_checked': True}))
finally:
    App.closeDocument(document.Name)
