"""Repair-budget control flow; actual kernels and services are covered separately."""
import pytest
from temporalio.exceptions import ApplicationError

from app.agent.durable_repair import decide_repair
from app.workflows.agent_v2 import McadAgentWorkflowV2


class CutSequence(McadAgentWorkflowV2):
    def __init__(self, failures):
        super().__init__()
        self.failures = iter(failures)
        self.repairs = []

    async def _activity(self, name, payload, **kwargs):
        if name == 'agent_v2.generate_operations':
            return {'source_id': 'original', 'source_hash': 'original', 'source_code': '{}'}
        if name == 'agent_v2.execute_freecad':
            identity = next(self.failures, None)
            if identity is None:
                return {'staging_manifest_id': 'new-result'}
            raise ApplicationError('cut removed no material', {
                'category': 'validation', 'error_code': 'subtractive_feature_no_effect',
                'operation_id': identity}, type='subtractive_feature_no_effect')
        assert name == 'agent_v2.repair_operations'
        self.repairs.append(payload)
        failure = payload['failure']
        decision = decide_repair(category=failure['category'], error_code=failure['error_code'],
            error_message=failure['error_message'], runtime_error_type=None,
            repair_count=payload.get('cut_operation_repair_index', payload['repair_index']) - 1,
            operation_id=failure['operation_id'], seen_signatures=payload['seen_signatures'])
        if not decision.repairable:
            raise ApplicationError(decision.reason, type='agent_freecad_repair_not_allowed')
        return {'source_id': 'repair', 'source_hash': 'new', 'source_code': '{}',
                'signature': decision.signature, 'repair_step_key': f"repair-{len(self.repairs)}"}


async def run_sequence(flow, enabled=True):
    return await flow._freecad_model_step(
        request={'scoped_repair_feedback_v1': True} if enabled else {},
        plan={'objective': 'two pockets', 'design_brief': {}},
        candidate_build_id='candidate', step={'step_key': 'model'}, step_index=0,
        requirements={}, base_state=None)


@pytest.mark.asyncio
async def test_distinct_failed_cuts_each_receive_their_local_attempt():
    flow = CutSequence(['first', 'second'])
    result = await run_sequence(flow)
    assert result['repair_count'] == 2
    assert [p['cut_operation_repair_index'] for p in flow.repairs] == [1, 1]
    assert [p['repair_index'] for p in flow.repairs] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize('failures,enabled,error,count', [
    (['first', 'first'], True, 'agent_freecad_repair_not_allowed', 2),
    (['first', 'second', 'third'], True, 'subtractive_feature_no_effect', 2),
    (['first', 'second'], False, 'agent_freecad_repair_not_allowed', 2),
])
async def test_repeated_cut_global_budget_and_legacy_history_remain_bounded(failures, enabled, error, count):
    flow = CutSequence(failures)
    with pytest.raises(ApplicationError) as raised:
        await run_sequence(flow, enabled)
    assert raised.value.type == error
    assert len(flow.repairs) == count
    if not enabled:
        assert all('cut_operation_repair_index' not in p for p in flow.repairs)


class ProgressiveSequence(CutSequence):
    async def _activity(self, name, payload, **kwargs):
        if name != 'agent_v2.execute_freecad' and name != 'agent_v2.repair_operations':
            return await super()._activity(name, payload, **kwargs)
        if name == 'agent_v2.repair_operations':
            self.repairs.append(payload)
            return {'source_id': 'repair', 'source_hash': 'new', 'source_code': '{}',
                'signature': str(len(self.repairs)), 'repair_step_key': f'repair-{len(self.repairs)}'}
        current = next(self.failures, None)
        if current is None:
            return {'staging_manifest_id': 'new-result'}
        raise ApplicationError(current['error_message'], {'category': 'validation', **current}, type=current['error_code'])


@pytest.mark.asyncio
async def test_progressive_workflow_uses_native_target_evidence_and_keeps_budget_across_turns():
    import json
    from tests.test_freecad_repair_progress import failure, POLICY
    failures = [failure('cut1'), failure('cut2', ['cut1']), failure('cut3', ['cut1', 'cut2']),
        failure('tip', ['cut1', 'cut2', 'cut3'], family='constraint', target='Tip'),
        failure('tip', ['cut1', 'cut2', 'cut3'])]
    failures[3]['error_code'] = 'sketch_under_constrained'
    for row in failures:
        row['details']['native_repair_policy_json'] = json.dumps(POLICY)
    flow = ProgressiveSequence(failures)
    async def execute():
        return await flow._freecad_model_step(request={'scoped_repair_feedback_v1': True, 'progressive_repair_v1': True},
            plan={'objective': 'multiple cuts', 'design_brief': {}}, candidate_build_id='candidate',
            step={'step_key': 'model'}, step_index=0, requirements={}, base_state=None)
    result = await execute()
    assert result['repair_count'] == 5
    assert [row['target_repair_index'] for row in flow.repairs] == [1] * 5
    assert flow._native_repair_budget.used == 5
    more = failure('later')
    more['details']['native_repair_policy_json'] = json.dumps({**POLICY, 'max_attempts': 1})
    flow.failures = iter([more])
    await execute()
    assert flow._native_repair_budget.used == 6
    assert flow.repairs[-1]['native_repair_policy'] == POLICY


@pytest.mark.asyncio
@pytest.mark.parametrize('same,limit,code', [
    (True, 8, 'agent_freecad_repair_no_progress'), (False, 1, 'agent_freecad_repair_budget_exhausted')])
async def test_progressive_workflow_stops_before_provider_when_evidence_or_budget_is_exhausted(same, limit, code):
    import json
    from tests.test_freecad_repair_progress import failure, POLICY
    failures = [failure('first'), failure('first' if same else 'second', ['first'])]
    for row in failures:
        row['details']['native_repair_policy_json'] = json.dumps({**POLICY, 'max_attempts': limit})
    flow = ProgressiveSequence(failures)
    with pytest.raises(ApplicationError) as raised:
        await flow._freecad_model_step(request={'scoped_repair_feedback_v1': True, 'progressive_repair_v1': True},
            plan={'objective': 'multiple cuts', 'design_brief': {}}, candidate_build_id='candidate',
            step={'step_key': 'model'}, step_index=0, requirements={}, base_state=None)
    assert raised.value.type == code and len(flow.repairs) == 1
    assert '当前失败操作' in raised.value.message and 'subtractive_feature_no_effect' in raised.value.message
    # Temporal serializes Python's explicit cause. The resource stop is the
    # task outcome; the native failure remains its linked modeling evidence.
    assert McadAgentWorkflowV2._root_application_error(raised.value).type == code


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,enabled', [('failed', True), ('indeterminate', True), ('indeterminate', False)])
@pytest.mark.parametrize('error_code', ['acceptance_scope_invalid', 'acceptance_measurement_failed'])
async def test_invalid_required_selection_never_requests_cad_source_repair(outcome, enabled, error_code):
    from tests.test_acceptance_validation_repairs import GateRun, arguments
    from tests.test_cad_intelligence_layers import _sample_report

    class SelectionFailure(GateRun):
        async def _activity(self, name, payload, **kwargs):
            self.calls.append(name)
            assert name == 'agent_v2.validate_geometry', 'invalid selectors cannot be fixed by CAD generation'
            report = _sample_report().model_dump(mode='json')
            report.update(outcome=outcome, acceptance={'contract_sha256': '0'*64, 'evidence': [
                {'check_id': 'gap', 'method': 'face_selection', 'outcome': 'indeterminate',
                 'issues': ['unverified measurement'], 'details': {'error_code': error_code, 'cad_source_repair_allowed':False}},
                {'check_id': 'solid', 'method': 'brep_solids', 'outcome': 'failed', 'measured': [2]},
            ]})
            return {'report': report, 'outcome': outcome, 'evidence_id': 'evidence'}

    flow = SelectionFailure()
    flow._scoped_repair_feedback_v1 = enabled
    args = arguments('geometry')
    args['plan']['design_brief']['acceptance'] = {'checks': [
        {'check_id': 'gap', 'required': True}, {'check_id': 'solid', 'required': True}]}
    with pytest.raises(ApplicationError) as raised:
        await flow._geometry_gate(**args, expected_dimensions={})
    assert raised.value.type == ('agent_'+error_code if enabled else 'agent_geometry_validation_failed')
    assert flow.calls == ['agent_v2.validate_geometry']
