"""Exercise the report gate against truncated, skipped and conflicting evidence."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'require_test_report.py'
spec = importlib.util.spec_from_file_location('test_report_gate', MODULE)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class TestReportGate(unittest.TestCase):
    def report(self, body):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'junit.xml'
        path.write_text('<testsuites><testsuite>' + body + '</testsuite></testsuites>')
        return path

    def case(self, identity, outcome=''):
        return '<testcase classname="contract" name="' + identity + '"><properties><property name="ci_nodeid" value="' + identity + '"/></properties>' + outcome + '</testcase>'

    def test_all_required_identities_pass(self):
        self.assertEqual(gate.validate(self.report(self.case('one') + self.case('two')), ['one', 'two']), 2)

    def test_unrelated_nonempty_report_fails(self):
        with self.assertRaisesRegex(ValueError, 'missing mandatory'):
            gate.validate(self.report(self.case('unrelated')), ['required'])

    def test_missing_parameterized_case_fails(self):
        with self.assertRaisesRegex(ValueError, 'missing mandatory'):
            gate.validate(self.report(self.case('case[positive]')), ['case[positive]', 'case[negative]'])

    def test_failure_error_and_skip_fail(self):
        for outcome in ('failure', 'error', 'skipped'):
            with self.subTest(outcome=outcome), self.assertRaisesRegex(ValueError, 'did not pass'):
                gate.validate(self.report(self.case('one', '<' + outcome + '/>')), ['one'])

    def test_optional_skips_never_count_as_passed(self):
        self.assertEqual(gate.validate(self.report(self.case('required') + self.case('optional', '<skipped/>')), ['required'], allow_skips=True), 1)

    def test_allow_skips_cannot_waive_required_case(self):
        with self.assertRaisesRegex(ValueError, 'did not pass'):
            gate.validate(self.report(self.case('required', '<skipped/>')), ['required'], allow_skips=True)

    def test_duplicate_receipts_fail(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            gate.validate(self.report(self.case('one') * 2), ['one'])

    def test_only_declared_skip_reason_is_allowed(self):
        path = self.report(self.case('external', '<skipped message="dedicated external fixture"/>') + self.case('required'))
        self.assertEqual(gate.validate(path, ['external', 'required'], allowed_skips={'external': 'dedicated external fixture'}), 1)
        with self.assertRaisesRegex(ValueError, 'unexpected skip reason'):
            gate.validate(path, ['external', 'required'], allowed_skips={'external': 'a different condition'})
        with self.assertRaisesRegex(ValueError, 'missing mandatory'):
            gate.validate(self.report(self.case('required')), ['external', 'required'], allowed_skips={'external': 'dedicated external fixture'})

    def test_empty_report_fails(self):
        with self.assertRaisesRegex(ValueError, 'no test cases'):
            gate.validate(self.report(''), ['one'])

    def test_ambiguous_identity_fails(self):
        body = '<testcase name="one"><properties><property name="ci_nodeid" value="one"/><property name="ci_nodeid" value="two"/></properties></testcase>'
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            gate.validate(self.report(body), ['one'])


if __name__ == '__main__':
    unittest.main()
