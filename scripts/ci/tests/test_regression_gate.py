import json
from pathlib import Path
import tempfile
import unittest
import hashlib

from scripts.ci.require_regression import validate_jobs, validate_metadata
from scripts.ci.require_runtime_evidence import validate


class RegressionGate(unittest.TestCase):
    def test_lifecycle_requires_its_own_successful_real_history(self):
        contract = {'history.json': {'kind': 'workflow-replay', 'families': ['ModelJobWorkflow']}}
        row = {'workflow_type': 'ModelJobWorkflow', 'workflow_id': 'model-job', 'run_id': 'run', 'events': 12, 'patches': [], 'replay': 'passed'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for invalid in ([], [{**row, 'workflow_type': 'McadDurableWorkflow'}], [{**row, 'replay': 'failed'}], [{**row, 'events': 1}], [{**row, 'run_id': ''}]):
                (root / 'history.json').write_text(json.dumps(invalid))
                with self.subTest(history=invalid), self.assertRaises(ValueError):
                    validate(root, contract)
            (root / 'history.json').write_text(json.dumps([row]))
            self.assertEqual(validate(root, contract), 1)

    def test_every_job_must_succeed(self):
        validate_jobs({'core': {'result': 'success'}, 'browser': {'result': 'success'}}, ['core', 'browser'])
        for outcome in ('failure', 'skipped', 'cancelled', None):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                validate_jobs({'core': {'result': outcome}}, ['core'])

    def test_missing_job_fails(self):
        with self.assertRaises(ValueError):
            validate_jobs({'quick': {'result': 'success'}}, ['quick', 'core'])

    def test_quick_success_cannot_replace_full_jobs(self):
        with self.assertRaises(ValueError):
            validate_jobs({'quick': {'result': 'success'}}, ['core', 'browser', 'lifecycle'])

    def test_stale_commit_run_or_attempt_fails(self):
        expected = {'source_sha': 'a' * 40, 'run_id': '100', 'run_attempt': '2'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'core').mkdir()
            for field in expected:
                record = {'job': 'core', **expected, field: 'different'}
                (root / 'core/job.json').write_text(json.dumps(record))
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'stale'):
                    validate_metadata(root, ['core'], expected)
            (root / 'core/job.json').write_text(json.dumps({'job': 'core', **expected}))
            validate_metadata(root, ['core'], expected)

    def test_missing_native_report_fails(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, 'missing mandatory evidence'):
            validate(Path(directory), {'native.log': {'kind': 'marker', 'marker': 'DONE'}})

    def test_native_python_exception_fails_even_with_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'native.log').write_text('DONE\nTraceback (most recent call last)\n')
            with self.assertRaisesRegex(ValueError, 'did not finish'):
                validate(root, {'native.log': {'kind': 'marker', 'marker': 'DONE'}})

    def test_truthy_value_cannot_impersonate_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'result.json').write_text('{"passed": 1}')
            with self.assertRaisesRegex(ValueError, 'missing successful evidence'):
                validate(root, {'result.json': {'kind': 'json', 'fields': {'passed': True}}})

    def test_dynamic_replay_requires_every_family_and_patch_generation(self):
        contract = {'replay.json': {'kind': 'workflow-replay', 'families': ['McadAgentWorkflowV2', 'ModelJobWorkflow'], 'pre_post_model_jobs': True}}
        rows = [{'workflow_type': 'McadAgentWorkflowV2', 'workflow_id': 'v2-old', 'run_id': 'one', 'events': 12, 'patches': [], 'replay': 'passed'},
                {'workflow_type': 'McadAgentWorkflowV2', 'workflow_id': 'v2-new', 'run_id': 'two', 'events': 12, 'patches': ['agent-v2-model-jobs-v1'], 'replay': 'passed'},
                {'workflow_type': 'ModelJobWorkflow', 'workflow_id': 'job', 'run_id': 'three', 'events': 12, 'patches': [], 'replay': 'passed'}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for incomplete in ([], rows[:2], rows[1:]):
                (root / 'replay.json').write_text(json.dumps(incomplete))
                with self.assertRaises(ValueError):
                    validate(root, contract)
            (root / 'replay.json').write_text(json.dumps(rows))
            self.assertEqual(validate(root, contract), 1)

    def test_step_receipt_requires_every_command_and_unchanged_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);log=root/'core.log';log.write_text('actual output')
            row={'step':'core','log':'core.log','command':['python','test.py'],'exit_code':0,'sha256':hashlib.sha256(log.read_bytes()).hexdigest()}
            (root/'steps.json').write_text(json.dumps([row]))
            self.assertEqual(validate(root,{'steps.json':{'kind':'steps','steps':['core']}}),1)
            with self.assertRaises(ValueError):validate(root,{'steps.json':{'kind':'steps','steps':['core','native']}})
            row['exit_code']=False
            (root/'steps.json').write_text(json.dumps([row]))
            with self.assertRaisesRegex(ValueError,'invalid step receipt'):validate(root,{'steps.json':{'kind':'steps','steps':['core']}})
            row['exit_code']=0
            (root/'steps.json').write_text(json.dumps([row]))
            log.write_text('changed')
            with self.assertRaisesRegex(ValueError,'log changed'):validate(root,{'steps.json':{'kind':'steps','steps':['core']}})

    def test_frozen_replay_cannot_drop_a_released_history(self):
        from scripts.ci.require_runtime_evidence import ROOT
        path='backend/tests/fixtures/released-histories/manifest.json'
        rows=json.loads((ROOT/path).read_text())['histories']
        report=[{key:r[key] for key in ('file','source_sha','sha256','cases')} for r in rows]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'replay.json').write_text(json.dumps({'passed':True,'histories':report[:-1]}))
            with self.assertRaisesRegex(ValueError,'omitted or changed'):validate(root,{'replay.json':{'kind':'released-replay','manifest':path}})


if __name__ == '__main__':
    unittest.main()
