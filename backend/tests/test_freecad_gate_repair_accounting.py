"""Workflow accounting contracts; kernel/service execution has separate coverage."""
import pytest
from temporalio.exceptions import ApplicationError

from app.workflows.work_accounting import WorkAccounting, WorkBudgetPolicy
from tests.test_acceptance_validation_repairs import GateRun, arguments
from tests.test_cad_intelligence_layers import _sample_report


class GeometrySequence(GateRun):
    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = iter(outcomes)
        self._repair_accounting_v2 = True

    async def _activity(self, name, payload, **kwargs):
        if name != 'agent_v2.validate_geometry':
            return await super()._activity(name, payload, **kwargs)
        self.calls.append((name, payload, kwargs))
        outcome = next(self.outcomes)
        report = _sample_report().model_dump(mode='json')
        report.update(outcome=outcome)
        return {'outcome': outcome, 'report': report, 'evidence_id': str(len(self.calls))}


class FailedGeometrySequence(GeometrySequence):
    async def _activity(self, name, payload, **kwargs):
        result = await super()._activity(name, payload, **kwargs)
        if name == 'agent_v2.validate_geometry':
            result['report'].update(
                issues=['width axis mismatch'],
                expected_dimensions_mm={'width': 200.0},
            )
            result['report']['artifacts'][0].update(
                dimensions_mm=[100.0, 100.0, 200.0],
                issues=['expected width 200.0 mm'],
            )
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize('native_repairs', [0, 1, 2, 5])
async def test_geometry_has_its_own_budget_after_native_repairs(native_repairs):
    flow = GeometrySequence(['failed', 'failed', 'passed'])
    args = arguments('geometry', budget=2)
    args['modeled']['repair_count'] = native_repairs
    result = await flow._geometry_gate(**args, expected_dimensions={})
    repairs = [p for n, p, _ in flow.calls if n == 'agent_v2.repair_operations']
    assert [p['gate_repair_index'] for p in repairs] == [1, 2]
    assert [p['repair_index'] for p in repairs] == [native_repairs + 1, native_repairs + 2]
    assert result['repair_count'] == native_repairs + 2


@pytest.mark.asyncio
async def test_geometry_revalidation_does_not_reset_stage_budget_or_audit_sequence():
    flow = GeometrySequence(['failed', 'passed', 'failed', 'passed', 'failed'])
    args = arguments('geometry', budget=2)
    args['modeled']['repair_count'] = 5
    args['modeled'] = await flow._geometry_gate(**args, expected_dimensions={})
    # An intervening visual repair has a distinct audit sequence.
    args['modeled']['repair_count'] += 1
    args['modeled'] = await flow._geometry_gate(**args, expected_dimensions={}, validation_cycle=1)
    with pytest.raises(ApplicationError) as stopped:
        await flow._geometry_gate(**args, expected_dimensions={}, validation_cycle=2)
    assert stopped.value.type == 'agent_geometry_validation_failed'
    assert stopped.value.details[0]['budget_name'] == 'geometry:model'
    assert stopped.value.details[0]['budget_used'] == 2
    repairs = [p for n, p, _ in flow.calls if n == 'agent_v2.repair_operations']
    assert [(p['repair_index'], p['gate_repair_index']) for p in repairs] == [(6, 1), (8, 2)]


@pytest.mark.asyncio
async def test_legacy_geometry_accounting_is_unchanged():
    flow = GeometrySequence(['failed'])
    flow._repair_accounting_v2 = False
    args = arguments('geometry', budget=2)
    args['modeled']['repair_count'] = 5
    with pytest.raises(ApplicationError):
        await flow._geometry_gate(**args, expected_dimensions={})
    assert len(flow.calls) == 1


@pytest.mark.asyncio
async def test_geometry_failure_exposes_report_diagnostics_in_application_error():
    flow = FailedGeometrySequence(['failed', 'failed'])
    args = arguments('geometry', budget=1)
    with pytest.raises(ApplicationError, match='width axis mismatch') as raised:
        await flow._geometry_gate(**args, expected_dimensions={})
    assert raised.value.type == 'agent_geometry_validation_failed'
    details = raised.value.details[0]
    assert details['report_issues'] == ['width axis mismatch']
    assert details['expected_dimensions_mm'] == {'width': 200.0}
    assert details['measured_artifacts'][0]['dimensions_mm'] == [100.0, 100.0, 200.0]


def test_modification_target_dimensions_reach_geometry_gate_contract():
    from app.workflows.agent_v2 import _expected_geometry_dimensions

    assert _expected_geometry_dimensions({
        'target_params': {'width': 200.0, 'Pad.Length': 200.0},
    }) == {'width': 200.0}


def test_total_budget_and_stage_counters_are_distinct_and_replay_deterministic():
    policy = WorkBudgetPolicy(max_repair_attempts=7, max_work_units=2, max_checkpoint_turns=2)
    for_replay = []
    for _ in range(2):
        accounting = WorkAccounting(policy)
        for n in range(5):
            assert accounting.reserve_repair(str(n), 'model', 'native') == n + 1
        assert accounting.used('model', 'geometry') == 0
        assert accounting.reserve_repair('g1', 'model', 'geometry') == 6
        assert accounting.reserve_repair('g1', 'model', 'geometry') == 6
        assert accounting.reserve_repair('g2', 'component', 'geometry') == 7
        assert accounting.used('model', 'geometry') == 1
        with pytest.raises(ApplicationError) as stopped:
            accounting.reserve_repair('g3', 'model', 'geometry')
        assert stopped.value.details[0]['budget_name'] == 'total_repair_attempts'
        for n in range(2):
            accounting.reserve_work(str(n))
            accounting.reserve_work(str(n))
            accounting.reserve_checkpoint(str(n))
        with pytest.raises(ApplicationError):
            accounting.reserve_work('third')
        with pytest.raises(ApplicationError):
            accounting.reserve_checkpoint('third')
        for_replay.append(accounting.snapshot())
    assert for_replay[0] == for_replay[1]


@pytest.mark.asyncio
async def test_native_failure_after_gate_repair_uses_the_same_recovery_and_manifest():
    class FailedExport(GeometrySequence):
        async def _activity(self, name, payload, **kwargs):
            if name == 'agent_v2.execute_freecad' and not any(n == name for n, _, _ in self.calls):
                self.calls.append((name, payload, kwargs))
                raise ApplicationError('select final Body', {'category':'user_code', 'error_code':'invalid_export'},
                                       type='invalid_export')
            return await super()._activity(name, payload, **kwargs)

    flow = FailedExport(['failed','passed'])
    flow._gate_native_recovery = True
    args = arguments('geometry')
    args['modeled']['repair_count'] = 5
    result = await flow._geometry_gate(**args, expected_dimensions={})
    repairs = [p for n,p,_ in flow.calls if n == 'agent_v2.repair_operations']
    executions = [p for n,p,_ in flow.calls if n == 'agent_v2.execute_freecad']
    assert [(p['repair_index'], p['gate_repair_index']) for p in repairs] == [(6,1),(7,1)]
    assert [p['supersedes_staging_manifest_id'] for p in executions] == ['old-manifest','old-manifest']
    assert result['repair_count'] == 7 and result['executed']['staging_manifest_id'] == 'new-manifest'
