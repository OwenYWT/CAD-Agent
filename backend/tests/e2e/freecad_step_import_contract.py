"""Real STEP transfer regression; contains no customer geometry or fake kernel."""
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, '/opt/cad-agent')
import FreeCAD as App
import Part
import freecad_entry as runner


INPUT = Path('/sandbox/input')
OUTPUT = Path('/sandbox/output')


def task(name, basename):
    return {'schema_version': 'mcad-capability-task.v1', 'capability': 'freecad', 'operation': 'execute',
        'inputs': {'base': name}, 'params': {'import_format': 'step', 'plan': {
            'schema_version': 'freecad-operation-plan.v1', 'document_name': 'StepRegression',
            'execution_mode': 'final', 'operations': [
                {'op_id': basename+'-inspect', 'action': 'document.inspect', 'args': {}},
                {'op_id': basename+'-export', 'action': 'document.export',
                 'args': {'formats': ['fcstd', 'step', 'stl'], 'basename': basename}}]}}}


def main():
    # OCCT's STEP transfer underestimates the precision of intersection curves.
    # A torus cut by a box reproduces InvalidCurveOnSurface in the locked kernel.
    shape = Part.makeTorus(10, 3).cut(Part.makeBox(10, 20, 10, App.Vector(0, -10, -5)))
    source = INPUT/'curved-import.step'
    shape.exportStep(str(source))
    before = Part.read(str(source))
    assert before.isValid() and before.Solids
    try:
        before.check(True)
    except ValueError as error:
        assert 'BOPAlgo_InvalidCurveOnSurface' in str(error)
    else:
        raise AssertionError('fixture no longer reproduces the locked STEP transfer defect')
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    result = runner.run_task(task(source.name, 'curved_import'))
    assert result['status'] == 'succeeded'
    evidence = result['source_baseline']['normalization']
    assert evidence['schema_version'] == 'step-import-normalization.v1'
    assert evidence['changed_edges'] and evidence['strict_check_passed']
    assert evidence['saved_strict_check_passed']
    assert evidence['geometry_before'] == evidence['geometry_after']
    opened = App.openDocument(result['files']['fcstd'])
    after = opened.ImportedBase.Shape
    assert not after.check(True)
    assert len(after.Solids) == len(before.Solids)
    assert len(after.Faces) == len(before.Faces) and len(after.Edges) == len(before.Edges)
    assert abs(after.Volume-before.Volume) < 1e-6
    App.closeDocument(opened.Name)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    # STEP readers rebuild tolerance metadata; the same bounded import path
    # must also handle a re-import of our delivered STEP, without drift.
    (INPUT/'roundtrip.step').write_bytes(Path(result['files']['step']).read_bytes())
    roundtrip = runner.run_task(task('roundtrip.step', 'roundtrip'))
    reopened = App.openDocument(roundtrip['files']['fcstd'])
    assert not reopened.ImportedBase.Shape.check(True)
    assert abs(reopened.ImportedBase.Shape.Volume-before.Volume) < 0.001
    App.closeDocument(reopened.Name)
    # The source is genuinely invalid for strict checking even though the
    # import path now handles it. Ordinary modeling checks must still reject it.
    doc = App.newDocument('UntrustedFeature')
    doc.addObject('Part::Feature', 'Untrusted').Shape = before
    try:
        runner._validate_document(doc, op_id='ordinary', action='api.execute')
    except runner.FreeCADRunnerError as error:
        assert error.code == 'shape_check_failed'
    else:
        raise AssertionError('ordinary modeling validation was weakened')
    App.closeDocument(doc.Name)
    # A scaled version has a real discrepancy beyond the one-micron import
    # policy. It must fail, keeping both the uploaded file and candidate empty.
    large = shape.copy()
    large.scale(1000)
    large_path = INPUT/'excessive-precision.step'
    large.exportStep(str(large_path))
    large_hash = hashlib.sha256(large_path.read_bytes()).hexdigest()
    try:
        runner.run_task(task(large_path.name, 'excessive'))
    except runner.FreeCADRunnerError as error:
        assert error.code == 'native_import_precision_unsupported', error.code
    else:
        raise AssertionError('excessive import precision was silently accepted')
    assert not (OUTPUT/'excessive.FCStd').exists()
    assert hashlib.sha256(large_path.read_bytes()).hexdigest() == large_hash
    print('CAD_STEP_IMPORT_CONTRACT='+json.dumps({
        'real_step_transfer_defect': True, 'source_unchanged': True,
        'strict_fcstd_reopen': True, 'step_reimport': True,
        'ordinary_model_validation_preserved': True, 'excessive_precision_rejected': True,
        'changed_edges': len(evidence['changed_edges'])}))


main()
