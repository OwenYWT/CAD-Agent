"""Real incident: independent no-op cuts, out-of-body profiles and downstream DOFs."""
import copy
import json
import math
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, '/opt/cad-agent')
import freecad_entry as runner
import FreeCAD as App
from freecad_cut_diagnostics import diagnose_cut, pending_profiles

fixture = json.loads(Path('/tests/fixtures/native_failures/multiple_cut_coordinates.json').read_text())
plan = copy.deepcopy(fixture['plan'])
reports = []
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    runner.INPUT_ROOT = root/'input'; runner.INPUT_ROOT.mkdir()
    runner.OUTPUT_ROOT = root/'output'; runner.OUTPUT_ROOT.mkdir()
    for target in ('pocket-throat', 'pocket-clamp', 'pocket-screw'):
        try:
            runner.run_task({'schema_version': 'mcad-capability-task.v1', 'capability': 'freecad',
                'operation': 'execute', 'inputs': {}, 'params': {'plan': plan}})
        except runner.FreeCADRunnerError as error:
            assert error.code == 'subtractive_feature_no_effect' and error.op_id == target
            assert 'cut_diagnostic_json' in error.details, {k: v for k, v in error.details.items() if 'snapshot' not in k}
            diagnostic = json.loads(error.details['cut_diagnostic_json'])
            snapshot = json.loads(error.details['failure_snapshot_json'])
            assert snapshot['valid_checkpoint'] is False and snapshot['failed_operation_id'] == target
            assert snapshot['failure']['details']['cut_diagnostic_json'] == error.details['cut_diagnostic_json']
            assert not list(runner.OUTPUT_ROOT.iterdir())
            if target != 'pocket-screw':
                assert diagnostic['classification'] == 'wrong_direction'
                assert diagnostic['intersection_volume_mm3'] == 0
                assert diagnostic['opposite_intersection_volume_mm3'] > 0
            else:
                assert diagnostic['classification'] == 'profile_outside_material'
                assert diagnostic['profile_bounds']['min'][2] == 17
                assert diagnostic['intersection_volume_mm3'] == diagnostic['opposite_intersection_volume_mm3'] == 0
                pending = json.loads(error.details['pending_profiles_json'])
                tip = next(item for item in pending['profiles'] if item['sketch'] == 'TipUpturn')
                assert tip['constraint_status'] == 'under_constrained'
                assert tip['profile_bounds']['min'][2] == 100
                assert tip['current_material_intersection_mm3'] == 0
                assert tip['status'] == 'diagnostic_only'
            reports.append({'operation': target, 'diagnostic': diagnostic})
        else:
            raise AssertionError('The unmodified incident must fail closed')
        next(op for op in plan['operations'] if op['op_id'] == target)['args']['reversed'] = True
measured = []
for normal, x_axis in [([0, 0, 1], [1, 0, 0]), ([0, -1, 0], [1, 0, 0]), ([1, 0, 0], [0, 1, 0])]:
    document = App.newDocument('CutDiagnosticFrames')
    try:
        for name, offset in [('Base', 0), ('Cut', 20)]:
            runner._sketch_create(document, {'name': name, 'frame': {
                'origin': dict(zip('xyz', [offset * v for v in normal])), 'normal': normal, 'x_axis': x_axis}})
            runner._sketch_add_profile(document, {'sketch': name, 'geometry': {
                'kind': 'circle', 'center': {'x': 0, 'y': 0}, 'radius_mm': 10 if name == 'Base' else 3}})
            document.recompute()
            if name == 'Base':
                runner._feature_pad(document, {'name': 'Pad', 'profile': name, 'length_mm': 20})
                document.recompute()
        body = document.getObject('Body')
        body.Placement = App.Placement(App.Vector(7, -3, 11), App.Rotation(App.Vector(1, 1, 1), 37))
        document.recompute()
        cut, base = document.getObject('Cut'), document.getObject('Pad').Shape
        wrong = diagnose_cut(cut, base, {'length_mm': 5, 'reversed': True}, 'feature.pocket')
        assert wrong['classification'] == 'wrong_direction'
        assert abs(wrong['opposite_intersection_volume_mm3'] - math.pi * 9 * 5) < 1e-5
        hole = diagnose_cut(cut, base, {'diameter_mm': 4, 'depth_mm': 5, 'cut': None}, 'feature.hole')
        assert abs(hole['intersection_volume_mm3'] - math.pi * 4 * 5) < 1e-5
        try:
            diagnose_cut(cut, base, {'diameter_mm': 4, 'depth_mm': 5,
                'cut': {'kind': 'counterbore', 'diameter_mm': 8, 'depth_mm': 2}}, 'feature.hole')
        except ValueError as error:
            assert 'complete native cutting envelope' in str(error)
        else:
            raise AssertionError('shaped holes cannot claim a cylindrical diagnostic')
        placement = App.Placement(cut.Placement)
        placement.Base += App.Vector(*normal) * 5
        cut.Placement = placement
        document.recompute()
        reach = diagnose_cut(cut, base, {'length_mm': 2}, 'feature.pocket')
        assert reach['classification'] == 'insufficient_reach'
        assert reach['intersection_volume_mm3'] == reach['opposite_intersection_volume_mm3'] == 0
        assert reach['through_intersection_volume_mm3'] > 0
        # Future construction is always isolated, even when its name already exists.
        raw = {'operations': [
            {'op_id': 'failed', 'action': 'feature.pocket', 'args': {'profile': 'Base'}},
            {'op_id': 'create', 'action': 'sketch.create', 'args': {'name': 'Cut'}},
            {'op_id': 'circle', 'action': 'sketch.add_profile', 'args': {'sketch': 'Cut', 'geometry': {
                'kind': 'circle', 'center': {'x': 0, 'y': 0}, 'radius_mm': 2}}},
            {'op_id': 'later', 'action': 'feature.pocket', 'args': {'profile': 'Cut', 'length_mm': 5}}]}
        dispatch = {'sketch.create': runner._sketch_create, 'sketch.add_profile': runner._sketch_add_profile}
        before = (list(cut.Placement.toMatrix().A), cut.ConstraintCount, len(document.Objects))
        preview = json.loads(pending_profiles(document, raw, raw['operations'][0], base, dispatch, body_name='Body'))
        assert preview['profiles'][0]['status'] == 'diagnostic_only'
        assert before == (list(cut.Placement.toMatrix().A), cut.ConstraintCount, len(document.Objects))
        assert App.ActiveDocument.Name == document.Name
        bounded = json.loads(pending_profiles(document, raw, raw['operations'][0], base, dispatch,
            body_name='Body', max_operations=0))
        assert bounded['profiles'][0]['status'] == 'unavailable'
        assert 'budget' in bounded['profiles'][0]['reason']
        truncated = json.loads(pending_profiles(document, raw, raw['operations'][0], base, dispatch,
            body_name='Body', max_profiles=0))
        assert truncated['truncated'] and not truncated['profiles']
        measured.append({'normal': normal, 'wrong_direction': True, 'insufficient_reach': True,
            'hole_effective_diameter': True, 'isolated_pending_profiles': True})
    finally:
        App.closeDocument(document.Name)
print('CAD_CUT_DIAGNOSTICS_CONTRACT=' + json.dumps({'retained_failures': reports,
    'no_candidate_from_failure': True, 'frame_cases': measured}))
