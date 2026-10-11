"""Real self-crossing boundary -> local patch -> parameter proof -> FCStd reopen."""
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, '/opt/cad-agent')
import freecad_entry as runner

payload = json.loads(Path('/sandbox/input/scoped-repair.json').read_text())
report = {}
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    runner.INPUT_ROOT = root/'input'; runner.INPUT_ROOT.mkdir()
    runner.OUTPUT_ROOT = root/'output'; runner.OUTPUT_ROOT.mkdir()

    def run(params):
        return runner.run_task({'schema_version': 'mcad-capability-task.v1', 'capability': 'freecad',
                               'operation': 'execute', 'inputs': {}, 'params': params})

    try:
        run({'plan': payload['original']})
    except runner.FreeCADRunnerError as exc:
        assert exc.code == 'profile_geometry_invalid'
        intersections = json.loads(exc.details['profile_intersections_json'])
        snapshot = json.loads(exc.details['failure_snapshot_json'])
        assert json.loads(snapshot['failure']['details']['profile_intersections_json']) == intersections
        assert any(item['geometry_indices'] == [2, 7] and item['point_local_mm'][:2] == [0, -17.5]
                   for item in intersections['intersections']), intersections
        report['actual_crossing_identified'] = True
    else:
        raise AssertionError('self-crossing original was accepted')
    try:
        run(payload['independent_anchor'])
    except runner.FreeCADRunnerError as exc:
        assert exc.code == 'sketch_parameter_dependency_failed'
        assert exc.details['logical_id'] == 'hook-dx-anchor'
        assert 'connectivity' in str(exc)
        assert not list(runner.OUTPUT_ROOT.iterdir())
        report['independent_anchor_breaks_connectivity'] = True
    else:
        raise AssertionError('independent structural offset bypassed parameter checks')
    result = run(payload['datum_anchor'])
    row = next(v for v in result['validations'] if v.get('gate') == 'profile_replan')
    evidence = json.loads(row['evidence_json'])
    assert evidence['saved_reopened'] and evidence['parameter_probes']
    assert all(p['status'] == 'passed' for p in evidence['parameter_probes'])
    doc = runner.App.openDocument(result['files']['fcstd'])
    try:
        assert doc.HookSketch.FullyConstrained
        assert doc.Body.Shape.isValid() and len(doc.Body.Shape.Solids) == 1
        report['parameter_probes'] = len(evidence['parameter_probes'])
        report['saved_reopened_single_solid'] = True
        if os.environ.get('SCOPED_STEP_OUTPUT'):
            runner.Part.export([doc.Body], os.environ['SCOPED_STEP_OUTPUT'])
    finally:
        runner.App.closeDocument(doc.Name)
print('CAD_SCOPED_REPAIR_CONTRACT=' + json.dumps(report))
