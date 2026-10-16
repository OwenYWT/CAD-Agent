"""Target-release admission uses real request schemas and rejects data loss."""
import importlib.util
import json
from pathlib import Path
import unittest
from uuid import uuid4

from app.models.workflow_requests import McadAgentWorkflowV2Request, McadCheckRequest


spec = importlib.util.spec_from_file_location(
    'inflight_probe', Path(__file__).resolve().parents[3] / 'deploy/tencent/check-inflight.py',
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class InflightContracts(unittest.TestCase):
    def check_request(self):
        request = McadCheckRequest(**{name: uuid4() for name in (
            'workflow_run_id', 'source_workflow_run_id', 'source_revision_id',
            'tenant_id', 'project_id', 'principal_id',
        )}, description='private user dimensions')
        return {'id': str(request.workflow_run_id), 'kind': 'mcad.check',
                'request_payload': request.temporal_payload()}

    def test_supported_check_keeps_all_serialized_fields(self):
        item = self.check_request()
        result = probe.inspect_requests([item])
        self.assertEqual(result, {'compatible': True, 'checked': 1, 'blockers': []})

    def test_supported_persistent_budget_contract_is_accepted(self):
        item = self.check_request()
        item['provider_budget_contract'] = 'provider-budget.v1'
        self.assertTrue(probe.inspect_requests([item])['compatible'])

    def test_unknown_persistent_budget_contract_blocks_old_target(self):
        item = self.check_request()
        item['provider_budget_contract'] = 'provider-budget.future'
        result = probe.inspect_requests([item])
        self.assertFalse(result['compatible'])
        self.assertEqual(result['blockers'], [{'workflow_id': item['id'],
            'reason': 'provider_budget_contract_unsupported'}])
        self.assertNotIn('private', json.dumps(result))

    def test_receipt_recovery_is_required_even_without_active_workflows(self):
        result = probe.inspect_requests([], receipt_recovery_contract='provider-receipt-recovery.v1')
        self.assertTrue(result['compatible'])
        from unittest.mock import patch
        with patch('app.contracts.provider_budget.SUPPORTED_PROVIDER_RECEIPT_CONTRACTS', frozenset()):
            result = probe.inspect_requests([], receipt_recovery_contract='provider-receipt-recovery.v1')
        self.assertEqual(result, {'compatible':False, 'checked':0,
            'blockers':[{'reason':'provider_receipt_recovery_unsupported'}]})

    def test_unknown_contract_cannot_pass_through_agent_prefix(self):
        request = McadAgentWorkflowV2Request(**{name: uuid4() for name in (
            'workflow_run_id', 'tenant_id', 'project_id', 'principal_id',
            'branch_id', 'expected_base_revision_id',
        )}, operation='generate', objective='private user dimensions')
        item = {'id': str(request.workflow_run_id), 'kind': 'mcad.agent.v2.future-operation',
                'request_payload': request.temporal_payload()}
        result = probe.inspect_requests([item])
        self.assertFalse(result['compatible'])
        self.assertEqual(result['blockers'][0]['reason'], 'unknown_workflow_contract')

    def test_unsupported_payload_is_rejected_without_user_content(self):
        item = self.check_request()
        item['request_payload']['future_acceptance'] = {'secret': 'private-test-value'}
        result = probe.inspect_requests([item])
        self.assertFalse(result['compatible'])
        self.assertEqual(result['blockers'][0]['reason'], 'ValidationError')
        self.assertNotIn('private', json.dumps(result))

    def test_deployment_requires_real_history_even_when_schema_is_valid(self):
        result = probe.inspect_requests([self.check_request()], require_histories=True)
        self.assertFalse(result['compatible'])
        self.assertEqual(result['blockers'][0]['reason'], 'workflow_history_incompatible')
        self.assertEqual(result['replayed_histories'], [])

    def test_replay_diagnostics_cannot_replace_machine_result(self):
        result = {'compatible': False, 'checked': 1, 'blockers': [{'reason': 'workflow_history_incompatible'}]}
        output = 'WARN [TMPRL1100] Nondeterminism error\n' + probe.RESULT_PREFIX + json.dumps(result) + '\n'
        self.assertEqual(probe.parse_response(output), result)
        with self.assertRaises(RuntimeError):
            probe.parse_response(output + probe.RESULT_PREFIX + json.dumps(result))
        with self.assertRaises(RuntimeError):
            probe.parse_response('WARN [TMPRL1100] Nondeterminism error\n')

    def test_nested_collection_loss_is_not_silently_accepted(self):
        before = {'checks': [{'limit': 1}, {'limit': 2}]}
        self.assertEqual(probe.missing_fields(before, {'checks': [{'limit': 1}]}), ['checks.1'])
        self.assertEqual(probe.missing_fields(before, {'checks': None}), ['checks'])

    def test_unknown_nested_policy_is_rejected_even_when_outer_schema_keeps_it(self):
        before = {'checks': [{'id': 'size', 'limits': {'max': 10}}]}
        after = {'checks': [{'id': 'size'}]}
        self.assertEqual(probe.missing_fields(before, after), ['checks.0.limits'])


if __name__ == '__main__':
    unittest.main()
