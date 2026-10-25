"""Real native FCStd -> STEP -> independent v2 predicate verification."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

import cadquery as cq
from feature_verification import measure_checks
from geometry_validation import validate_geometry_files


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    native = root / 'model.FCStd'
    step = root / 'model.step'
    program = f'''
import FreeCAD as App, Part
doc = App.newDocument('AcceptanceV2')
body = doc.addObject('PartDesign::Body', 'Body')
feature = body.newObject('PartDesign::Feature', 'Bracket')
feature.Shape = Part.makeBox(50, 35, 20).cut(Part.makeBox(40, 25, 20, App.Vector(10, 5, 0)))
doc.recompute()
assert feature.Shape.isValid() and len(feature.Shape.Solids) == 1
doc.saveAs({str(native)!r})
App.closeDocument(doc.Name)
reopened = App.openDocument({str(native)!r})
reopened.recompute()
Part.export([reopened.getObject('Body')], {str(step)!r})
print('NATIVE_ACCEPTANCE_V2_SAVED')
'''
    script = root / 'native.py'
    script.write_text(program)
    result = subprocess.run(['/opt/freecad/bin/FreeCADCmd', '-c',
        f"exec(compile(open({str(script)!r}).read(), {str(script)!r}, 'exec'))"], capture_output=True, text=True)
    assert result.returncode == 0 and 'NATIVE_ACCEPTANCE_V2_SAVED' in result.stdout and 'Traceback' not in result.stdout + result.stderr, result.stdout + result.stderr
    before = hashlib.sha256(step.read_bytes()).hexdigest()
    shape = cq.importers.importStep(str(step)).val()
    cases = [({'op': 'eq', 'value': 25., 'unit': 'mm'}, 'passed'),
             ({'op': 'eq', 'value': 20., 'unit': 'mm'}, 'failed'),
             ({'op': 'ge', 'lower': 5., 'unit': 'mm'}, 'passed'),
             ({'op': 'ge', 'lower': 26., 'unit': 'mm'}, 'failed'),
             ({'op': 'le', 'upper': 30., 'unit': 'mm'}, 'passed'),
             ({'op': 'le', 'upper': 24., 'unit': 'mm'}, 'failed'),
             ({'op': 'range', 'lower': 24., 'upper': 26., 'unit': 'mm'}, 'passed')]
    evidence = []
    for comparison, expected in cases:
        row = measure_checks(shape, [{'check_id': 'gap', 'kind': 'planar_gap', 'comparison': comparison}],
            acceptance_version='engineering-acceptance.v2', comparison_policy_id='occt-confusion-v1')[0]
        assert row['outcome'] == expected and abs(row['measured'][0] - 25) < 1e-7, row
        assert row['details']['comparison'] == comparison
        assert row['details']['verification_scope'] == 'cad_geometry'
        evidence.append(row)
    legacy = measure_checks(shape, [{'check_id': 'gap', 'kind': 'planar_gap', 'nominal': 25.}])[0]
    assert legacy['outcome'] == 'passed' and 'comparison' not in legacy['details']
    assert hashlib.sha256(step.read_bytes()).hexdigest() == before
    acceptance = {'schema_version':'engineering-acceptance.v2', 'objective':'夹口25mm',
        'comparison_policy_id':'occt-confusion-v1', 'verification_scope':'cad_geometry',
        'checks':[{'check_id':'gap','kind':'planar_gap','required':True,'scope':{'frame':'world'},
                   'comparison':{'op':'eq','value':25.,'unit':'mm'}}]}
    context = {'schema_version':'measurement-context.v1', 'base_revision_id':'native-base',
               'checkpoint_manifest_id':'native-current', 'artifact_sha256s':[before]}
    report = validate_geometry_files([{'role':'model','format':'step','path':str(step)}],
        acceptance=acceptance, measurement_context=context)
    assert report['outcome']=='passed', report
    binding = report['acceptance']['evidence'][0]['details']['measurement_binding']
    assert binding['artifact_sha256']==before and binding['status']=='bound'
    bad = {**context, 'artifact_sha256s':['0'*64]}
    try:
        validate_geometry_files([{'role':'model','format':'step','path':str(step)}], acceptance=acceptance, measurement_context=bad)
    except ValueError:
        pass
    else:
        raise AssertionError('wrong artifact hash accepted')
    assert hashlib.sha256(step.read_bytes()).hexdigest()==before
    # Topology is a separate obligation: equal diameter/count does not prove
    # that an opening reaches the other side. Reopen each actual FCStd before
    # independently reading its STEP (no native feature flags are consulted).
    program = f'''
import FreeCAD as App, Part
root = {str(root)!r}
plate = Part.makeBox(40, 30, 12)
axis = App.Vector(0, 0, 1)
def bore(z, depth, radius=3, x=20):
    return Part.makeCylinder(radius, depth, App.Vector(x, 15, z), axis)
through = plate.cut(bore(-1, 14))
shapes = {{
    'blind': plate.cut(bore(8, 5)),
    'through': through,
    'counterbore': through.cut(bore(9, 4, 5)),
    'countersink': through.cut(Part.makeCone(3, 5, 2, App.Vector(20, 15, 10))),
    'sealed_cavity': plate.cut(bore(4, 4)),
    'thin_web': plate.cut(bore(.01, 12)),
    'mixed': through.cut(bore(8, 5, x=10)),
}}
# A local through-wall passage opens into a real cavity. The opposite wall
# must remain intact; extending the test ray across the whole part is wrong.
enclosure = Part.makeBox(40, 30, 24).cut(Part.makeBox(34, 24, 21, App.Vector(3, 3, 3)))
side_bore = lambda x, length: Part.makeCylinder(3, length, App.Vector(x, 15, 12), App.Vector(1, 0, 0))
shapes['wall_through'] = enclosure.cut(side_bore(37, 4))
shapes['wall_blind'] = enclosure.cut(side_bore(38, 3))
shapes['wall_thin_web'] = enclosure.cut(side_bore(37.01, 4))
shapes['wall_both_sides'] = enclosure.cut(side_bore(-1, 42))
# A full round shaft between two closed internal chambers has two empty end
# sections but no connection to exterior. It is not a through opening.
chambers = Part.makeBox(8, 8, 8, App.Vector(8, 11, 2)).fuse(Part.makeBox(8, 8, 8, App.Vector(24, 11, 2)))
chambers = chambers.fuse(Part.makeCylinder(3, 16, App.Vector(12, 15, 6), App.Vector(1, 0, 0)))
shapes['sealed_chambers'] = plate.cut(chambers)
rotated = through.copy()
rotated.rotate(App.Vector(), App.Vector(1, 2, 3), 43)
rotated.translate(App.Vector(81, -32, 67))
shapes['rotated_through'] = rotated
for name, shape in shapes.items():
    doc = App.newDocument(name)
    feature = doc.addObject('PartDesign::Body', 'Body').newObject('PartDesign::Feature', 'Plate')
    feature.Shape = shape
    doc.recompute()
    assert shape.isValid() and len(shape.Solids) == 1
    doc.saveAs(root + '/' + name + '.FCStd')
    App.closeDocument(doc.Name)
    reopened = App.openDocument(root + '/' + name + '.FCStd')
    reopened.recompute()
    Part.export([reopened.getObject('Body')], root + '/' + name + '.step')
    App.closeDocument(reopened.Name)
print('NATIVE_THROUGH_CASES_SAVED')
'''
    script.write_text(program)
    result = subprocess.run(['/opt/freecad/bin/FreeCADCmd', '-c',
        f"exec(compile(open({str(script)!r}).read(), {str(script)!r}, 'exec'))"], capture_output=True, text=True)
    assert result.returncode == 0 and 'NATIVE_THROUGH_CASES_SAVED' in result.stdout and 'Traceback' not in result.stdout + result.stderr, result.stdout + result.stderr
    through_evidence = {}
    for name, expected in [('blind', 'failed'), ('through', 'passed'), ('counterbore', 'passed'),
                           ('countersink', 'passed'), ('sealed_cavity', 'failed'), ('thin_web', 'failed'),
                           ('mixed', 'failed'), ('rotated_through', 'passed'),
                           ('wall_through', 'passed'), ('wall_blind', 'failed'),
                           ('wall_thin_web', 'failed'), ('wall_both_sides', 'passed'),
                           ('sealed_chambers', 'failed')]:
        native_path, step_path = root / (name + '.FCStd'), root / (name + '.step')
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (native_path, step_path)]
        shape = cq.importers.importStep(str(step_path)).val()
        checks = [{'check_id': 'diameter', 'kind': 'hole_diameter', 'comparison': {'op': 'eq', 'value': 6., 'unit': 'mm'}},
                  {'check_id': 'through', 'kind': 'hole_through', 'comparison': {'op': 'eq', 'value': 1., 'unit': 'count'}}]
        rows = measure_checks(shape, checks, acceptance_version='engineering-acceptance.v2', comparison_policy_id='occt-confusion-v1')
        assert rows[0]['outcome'] == 'passed', (name, rows)
        assert rows[1]['outcome'] == expected, (name, rows)
        assert all(v == 1 for v in rows[1]['measured']) == (expected == 'passed'), (name, rows)
        if name in {'sealed_cavity', 'sealed_chambers'}:
            count = measure_checks(shape, [{'check_id': 'count', 'kind': 'hole_count',
                'comparison': {'op': 'eq', 'value': 1., 'unit': 'count'}}],
                acceptance_version='engineering-acceptance.v2', comparison_policy_id='occt-confusion-v1')[0]
            assert count['outcome'] == 'indeterminate', count
            assert count['details']['hole_inventory']['complete'] is False, count
        if name in {'wall_through', 'wall_both_sides'}:
            count = measure_checks(shape, [{'check_id':'one_side_hole', 'kind':'hole_count',
                'comparison':{'op':'eq','value':1.,'unit':'count'}, 'scope':{'axis':[1,0,0]}}],
                acceptance_version='engineering-acceptance.v2', comparison_policy_id='occt-confusion-v1')[0]
            assert count['outcome'] == ('passed' if name == 'wall_through' else 'failed'), count
        assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in (native_path, step_path)] == hashes
        through_evidence[name] = {'native_sha256': hashes[0], 'step_sha256': hashes[1], 'evidence': rows}
    print('CAD_ACCEPTANCE_COMPARISON_V2=' + json.dumps({'step_sha256': before, 'evidence': evidence,
        'through_evidence': through_evidence, 'v1_retained': True}))
