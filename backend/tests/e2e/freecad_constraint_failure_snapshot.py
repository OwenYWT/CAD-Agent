"""Real-kernel regression: failure evidence survives rollback, never as a model."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, '/opt/cad-agent')
import freecad_entry as runner


def plan():
    return {'schema_version': 'freecad-operation-plan.v1', 'document_name': 'Evidence',
        'operations': [
            {'op_id': 'sketch', 'action': 'sketch.create', 'args': {'name': 'Profile'}},
            {'op_id': 'circle', 'action': 'sketch.add_geometry', 'args': {
                'sketch': 'Profile', 'geometry': {'kind': 'circle',
                    'center': {'x': 8, 'y': 11}, 'radius_mm': 7}}},
            {'op_id': 'radius', 'action': 'sketch.add_constraint', 'args': {
                'sketch': 'Profile', 'kind': 'radius', 'first': {'geometry_index': 0}, 'value_mm': 7}},
            {'op_id': 'duplicate', 'action': 'sketch.add_constraint', 'args': {
                'sketch': 'Profile', 'kind': 'diameter', 'first': {'geometry_index': 0}, 'value_mm': 14}},
            {'op_id': 'pad', 'action': 'feature.pad', 'args': {
                'name': 'Pad', 'profile': 'Profile', 'length_mm': 9}},
            {'op_id': 'export', 'action': 'document.export', 'args': {'formats': ['fcstd', 'step']}},
        ]}


def main():
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        runner.INPUT_ROOT = root / 'input'; runner.INPUT_ROOT.mkdir()
        runner.OUTPUT_ROOT = root / 'output'; runner.OUTPUT_ROOT.mkdir()
        original = plan()
        task = {'schema_version': 'mcad-capability-task.v1', 'capability': 'freecad',
                'operation': 'execute', 'params': {'plan': original}, 'inputs': {}}
        try:
            runner.run_task(task)
        except runner.FreeCADRunnerError as exc:
            assert exc.code == 'sketch_redundant_constraints', exc.code
            raw = exc.details['failure_snapshot_json']
            evidence = json.loads(raw)
            assert hashlib.sha256(raw.encode()).hexdigest() == exc.details['failure_snapshot_sha256']
            assert evidence['schema_version'] == 'freecad-failure-snapshot.v1'
            assert evidence['valid_checkpoint'] is False
            assert evidence['failed_operation_id'] == 'duplicate'
            states = {o['operation_id']: o['status'] for o in evidence['operations']}
            assert states == {'sketch': 'executed', 'circle': 'executed', 'radius': 'executed',
                              'duplicate': 'failing', 'pad': 'not_executed', 'export': 'not_executed'}
            sketch = evidence['sketches'][0]
            assert sketch['name'] == 'Profile'
            assert sketch['solver']['constraint_status'] == 'redundant'
            assert len(sketch['geometry']) == 1
            assert [c['logical_id'] for c in sketch['constraints']] == ['radius', 'duplicate']
            assert all(c['driving'] for c in sketch['constraints'])
            assert evidence['plan_hash'] == hashlib.sha256(json.dumps(original,
                sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        else:
            raise AssertionError('Native redundant constraints must fail')
        assert not list(runner.OUTPUT_ROOT.glob('*.FCStd'))
        assert not list(runner.OUTPUT_ROOT.glob('*.step'))
        assert runner.App.ActiveDocument is None
        # Fault injection affects diagnostics only. The original native failure
        # and rollback must survive without publishing any artifacts.
        import freecad_failure_snapshot as snapshots
        original_capture = snapshots.capture_failure
        def broken_capture(*args, **kwargs):
            raise OSError('controlled diagnostic storage/extraction failure')
        snapshots.capture_failure = broken_capture
        try:
            try:
                runner.run_task(task)
            except runner.FreeCADRunnerError as exc:
                assert exc.code == 'sketch_redundant_constraints'
                assert exc.details['failure_snapshot_unavailable'] == 'OSError'
                assert 'failure_snapshot_json' not in exc.details
            else:
                raise AssertionError('diagnostic failure cannot turn a solver error into success')
            assert runner.App.ActiveDocument is None
            assert not list(runner.OUTPUT_ROOT.iterdir())
        finally:
            snapshots.capture_failure = original_capture
    print('CAD_CONSTRAINT_FAILURE_SNAPSHOT=' + json.dumps({'real_solver': True,
        'rollback_and_no_candidate': True, 'bound_evidence': True}))


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        import os, traceback
        traceback.print_exc(); os._exit(1)
