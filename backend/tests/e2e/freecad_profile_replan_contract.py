"""Native negative controls for every profile consumer, plus save/reopen geometry."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, '/opt/cad-agent')
sys.path.append('/tests')
from profile_replan_fixture import failed_plan, proposal
import freecad_entry as runner
from freecad_constraint_validation import validate_profile, ConstraintVerificationError

report = {'rejected': [], 'valid_boundaries': []}
with tempfile.TemporaryDirectory() as folder:
    folder = Path(folder)
    runner.INPUT_ROOT = folder / 'input'; runner.INPUT_ROOT.mkdir()
    runner.OUTPUT_ROOT = folder / 'output'; runner.OUTPUT_ROOT.mkdir()
    original = failed_plan()
    consumers = {
        'feature.pad': {'profile': 'hook_profile', 'length_mm': 40},
        'feature.pocket': {'profile': 'hook_profile', 'length_mm': 10},
        'feature.hole': {'profile': 'hook_profile', 'diameter_mm': 4, 'depth_mm': 10},
        'feature.loft': {'profiles': ['hook_profile', 'Other']},
        'feature.sweep': {'profile': 'hook_profile', 'path': 'Path'},
        'feature.revolve': {'profile': 'hook_profile', 'angle_deg': 90, 'axis': 'y'},
    }
    def run(plan, **params):
        return runner.run_task({'schema_version': 'mcad-capability-task.v1', 'capability': 'freecad',
            'operation': 'execute', 'inputs': {}, 'params': {'plan': plan, **params}})
    for action, args in consumers.items():
        plan = copy.deepcopy(original)
        if action != 'feature.pad':
            plan['operations'][-2] = {'op_id': 'consumer', 'action': action, 'args': {'name': 'Feature', **args}}
        try:
            run(plan)
        except runner.FreeCADRunnerError as exc:
            assert exc.code == 'profile_geometry_invalid', (action, exc.code, str(exc))
            assert 'SelfIntersect' in str(exc)
            snapshot = json.loads(exc.details['failure_snapshot_json'])
            assert snapshot['valid_checkpoint'] is False
            assert snapshot['failure']['details']['stage'] == 'profile_topology'
            assert snapshot['sketches'][0]['solver']['degrees_of_freedom'] == 0
            assert not list(runner.OUTPUT_ROOT.iterdir())
            report['rejected'].append(action)
            if action == 'feature.pad' and os.environ.get('PROFILE_DIAGNOSTIC_OUTPUT'):
                Path(os.environ['PROFILE_DIAGNOSTIC_OUTPUT']).write_text(json.dumps(snapshot, indent=2))
        else:
            raise AssertionError('invalid profile reached a feature')
    for offset, gap in [(0, 25), (17, 18), (-31, 30)]:
        plan = failed_plan()
        for op in plan['operations']:
            if op['action'] == 'sketch.add_profile':
                op['args']['geometry']['corner']['x'] += offset
        good = proposal(plan, gap=gap)
        result = run(good)
        doc = runner.App.openDocument(result['files']['fcstd'])
        try:
            assert doc.hook_profile.FullyConstrained
            shape = doc.HookBody.Shape
            assert shape.isValid() and len(shape.Solids) == 1
            assert abs(shape.Volume - (100*45-gap*40)*40) < 1e-5
            index = next(i for i, c in enumerate(doc.hook_profile.Constraints) if c.Type == 'Distance' and abs(c.Value-gap) < 1e-7)
            doc.hook_profile.setDatum(index, runner.App.Units.Quantity(f'{gap+1} mm'))
            doc.recompute()
            runner._validate_document(doc, op_id='test-parameter', action='profile.verify')
            assert doc.HookBody.Shape.isValid()
            assert abs(doc.HookBody.Shape.Volume - (100*45-(gap+1)*40)*40) < 1e-5
            saved = folder / f'edited-{offset}.FCStd'; doc.saveAs(str(saved))
        finally:
            runner.App.closeDocument(doc.Name)
        doc = runner.App.openDocument(str(saved))
        try:
            assert abs(doc.HookBody.Shape.Volume - (100*45-(gap+1)*40)*40) < 1e-5
        finally:
            runner.App.closeDocument(doc.Name)
        report['valid_boundaries'].append({'offset': offset, 'gap': gap, 'parameter_edit_reopened': True})
    # Open sweep paths also need topology checks; one connected wire alone is
    # insufficient when non-adjacent segments cross.
    doc = runner.App.newDocument('PathTopology')
    try:
        sketch = doc.addObject('Sketcher::SketchObject', 'Path')
        points = [(0, 0), (10, 10), (0, 10), (10, 0)]
        for a, b in zip(points, points[1:]):
            sketch.addGeometry(runner.Part.LineSegment(runner.App.Vector(*a, 0), runner.App.Vector(*b, 0)), False)
        doc.recompute()
        try:
            validate_profile(sketch, closed=False)
        except ConstraintVerificationError as exc:
            assert exc.code == 'profile_geometry_invalid'
            report['open_path_self_intersection'] = 'rejected'
        else:
            raise AssertionError('self-crossing sweep path must be rejected')
    finally:
        runner.App.closeDocument(doc.Name)
    # Native LinkSub selection is meaningful: the first edge is a valid pipe
    # path even when unused edges elsewhere in the sketch cross each other.
    doc = runner.App.newDocument('SelectedPath')
    try:
        body = doc.addObject('PartDesign::Body', 'Body')
        section = body.newObject('Sketcher::SketchObject', 'Section')
        section.addGeometry(runner.Part.Circle(runner.App.Vector(), runner.App.Vector(0, 0, 1), 1), False)
        section.Placement.Rotation = runner.App.Rotation(runner.App.Vector(0, 0, 1), runner.App.Vector(1, 1, 0))
        path = body.newObject('Sketcher::SketchObject', 'Path')
        points = [(0, 0), (10, 10), (0, 10), (10, 0)]
        for a, b in zip(points, points[1:]):
            path.addGeometry(runner.Part.LineSegment(runner.App.Vector(*a, 0), runner.App.Vector(*b, 0)), False)
        pipe = body.newObject('PartDesign::AdditivePipe', 'Pipe')
        pipe.Profile = (section, ['']); pipe.Spine = (path, ['Edge1'])
        doc.recompute()
        assert pipe.Shape.isValid() and len(pipe.Shape.Solids) == 1
        runner._validate_document(doc, op_id='selected-path', action='profile.verify')
        pipe.Spine = (path, ['']); doc.recompute()
        try:
            runner._validate_document(doc, op_id='whole-path', action='profile.verify')
        except runner.FreeCADRunnerError as exc:
            assert exc.code == 'profile_geometry_invalid'
        else:
            raise AssertionError('self-crossing whole path was accepted')
        report['selected_path_scope'] = 'passed'
    finally:
        runner.App.closeDocument(doc.Name)
    result = run(proposal(gap=32))
    doc = runner.App.openDocument(result['files']['fcstd'])
    try:
        index = next(i for i, c in enumerate(doc.hook_profile.Constraints) if c.Type == 'Distance' and abs(c.Value-32) < 1e-7)
        doc.hook_profile.setDatum(index, runner.App.Units.Quantity('33 mm'))
        doc.recompute()
        assert doc.hook_profile.Constraints[index].Value == 33
        assert doc.hook_profile.FullyConstrained and doc.HookBody.Shape.isValid()
        try:
            runner._validate_document(doc, op_id='invalid-parameter', action='profile.verify')
        except runner.FreeCADRunnerError as exc:
            assert exc.code == 'profile_geometry_invalid'
            report['invalid_parameter_rejected'] = exc.code
        else:
            raise AssertionError('valid solver and solid must not hide a self-intersecting consumed profile')
    finally:
        runner.App.closeDocument(doc.Name)
    # The typed parameter-edit entry must enforce the same check and roll back
    # before exporting a candidate. The retained base file must remain intact.
    import hashlib
    import shutil
    base = runner.INPUT_ROOT / 'base.FCStd'
    shutil.copyfile(result['files']['fcstd'], base)
    original_bytes = hashlib.sha256(base.read_bytes()).hexdigest()
    runner.OUTPUT_ROOT = folder / 'edit-output'; runner.OUTPUT_ROOT.mkdir()
    edit = {'schema_version': original['schema_version'], 'document_name': original['document_name'],
        'operations': [
            {'op_id': 'invalid-edit', 'action': 'sketch.set_constraint', 'args': {
                'sketch': 'hook_profile', 'constraint_index': index, 'expected_type': 'Distance', 'value_mm': 33}},
            {'op_id': 'edit-export', 'action': 'document.export', 'args': {'formats': ['fcstd', 'step'], 'basename': 'edited'}}]}
    try:
        runner.run_task({'schema_version': 'mcad-capability-task.v1', 'capability': 'freecad',
            'operation': 'execute', 'inputs': {'base': base.name}, 'params': {'plan': edit}})
    except runner.FreeCADRunnerError as exc:
        assert exc.code == 'profile_geometry_invalid', (exc.code, str(exc))
        snapshot = json.loads(exc.details['failure_snapshot_json'])
        assert snapshot['valid_checkpoint'] is False and snapshot['failed_operation_id'] == 'invalid-edit'
        assert not list(runner.OUTPUT_ROOT.iterdir())
        assert hashlib.sha256(base.read_bytes()).hexdigest() == original_bytes
        report['parameter_edit_rollback'] = 'passed'
    else:
        raise AssertionError('invalid parameter edit exported a candidate')
print('CAD_PROFILE_REPLAN_CONTRACT=' + json.dumps(report))
