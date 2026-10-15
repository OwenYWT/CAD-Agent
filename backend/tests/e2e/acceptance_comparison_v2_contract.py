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
    print('CAD_ACCEPTANCE_COMPARISON_V2=' + json.dumps({'step_sha256': before, 'evidence': evidence, 'v1_retained': True}))
