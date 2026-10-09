"""Scope/protocol tests consume a retained real native diagnostic, not a fake kernel."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.freecad.contracts import FreeCADOperationPlan
from app.freecad import profile_replan as replan
from app.freecad.failure_snapshot import digest
from app.freecad.operation_generator import FreeCADOperationGenerator
from tests.e2e.profile_replan_fixture import failed_plan, proposal, acceptance
from tests.test_freecad_agent_tools import ProviderTurns, call
from tests.test_freecad_operation_generator import _provenance


def inputs():
    before = FreeCADOperationPlan.model_validate(failed_plan())
    snapshot = json.loads((Path(__file__).parent / 'e2e/fixtures/native_failures/touching_profile_diagnostic.json').read_text())
    failure = {'category': 'validation', 'error_code': replan.PROFILE_ERROR, 'operation_id': 'pad-hook',
        'error_message': snapshot['failure']['message'], 'details': snapshot['failure']['details'],
        'engineering_acceptance': acceptance()}
    return before, snapshot, failure


def context():
    return replan.build_context(*inputs())


def test_native_profile_failure_has_separate_scope_and_fixed_acceptance():
    ctx = context()
    good = FreeCADOperationPlan.model_validate(proposal())
    certificate = replan.make_contract(good, ctx, {'max_parameter_probes': 128, 'perturbation_fraction': 0.01})
    replan.verify_contract(good, certificate, acceptance(), None)
    from app.freecad.repair_validation import NativeConstraintRepairValidation
    NativeConstraintRepairValidation().verify_profile_contract(good, certificate, acceptance(), None)
    assert certificate['sketch'] == 'hook_profile'
    assert certificate['acceptance_hash'] == digest(ctx['acceptance'])


@pytest.mark.parametrize('change', ['feature', 'frame', 'export', 'mode', 'bounds', 'unchanged', 'ids-only', 'other-sketch'])
def test_replan_cannot_change_protected_source(change):
    ctx = context(); raw = proposal()
    if change == 'feature': raw['operations'][-2]['args']['length_mm'] += 1
    elif change == 'frame': raw['operations'][0]['args']['frame']['origin']['x'] += 1
    elif change == 'export': raw['operations'][-1]['args']['basename'] = 'other'
    elif change == 'mode': raw['execution_mode'] = 'final'
    elif change == 'bounds': raw['operations'][1]['args']['geometry']['start']['x'] -= 1
    elif change in {'unchanged', 'ids-only'}:
        raw = failed_plan()
        if change == 'ids-only':
            raw['operations'][1]['op_id'] = 'renamed'
    else: raw['operations'][1]['args']['sketch'] = 'Another'
    with pytest.raises(ValueError):
        replan.validate_proposal(FreeCADOperationPlan.model_validate(raw), ctx)


@pytest.mark.parametrize('change', ['code', 'stage', 'plan-hash', 'acceptance', 'external', 'unknown', 'checkpoint', 'prior-proof'])
def test_missing_or_protected_evidence_never_grants_replan_permission(change):
    before, snapshot, failure = inputs(); prior = None
    if change == 'code': failure['error_code'] = 'constraint_repair_verification_failed'
    elif change == 'stage': snapshot['failure']['details']['stage'] = 'acceptance'
    elif change == 'plan-hash': snapshot['plan_hash'] = '0'*64
    elif change == 'acceptance': failure['engineering_acceptance'] = None
    elif change == 'external': snapshot['sketches'][0]['external_geometry'] = [['Other', ['Edge1']]]
    elif change == 'unknown': snapshot['sketches'][0]['constraints'][0]['origin'] = 'unknown'
    elif change == 'checkpoint': snapshot['checkpoint_operations']['create-sketch'] = snapshot['operations'][0]['operation_hash']
    else: prior = {'sketches': {'hook_profile': {}}}
    with pytest.raises(ValueError):
        replan.build_context(before, snapshot, failure, prior_contract=prior)


def test_frozen_contract_and_checkpoint_are_reverified_before_execution():
    good = FreeCADOperationPlan.model_validate(proposal())
    certificate = replan.make_contract(good, context(), {'max_parameter_probes': 128, 'perturbation_fraction': 0.01})
    altered = deepcopy(acceptance()); altered['checks'][0]['nominal'] = 24.0
    with pytest.raises(ValueError, match='acceptance'):
        replan.verify_contract(good, certificate, altered, None)
    with pytest.raises(ValueError, match='checkpoint'):
        replan.verify_contract(good, certificate, acceptance(), '0'*64)
    with pytest.raises(ValueError, match='missing'):
        replan.verify_receipt({'result': {'status': 'succeeded'}}, certificate)


def test_no_progress_fingerprint_ignores_edge_order_and_direction():
    _, snapshot, _ = inputs(); changed = deepcopy(snapshot)
    geometry = changed['sketches'][0]['geometry']
    geometry.reverse()
    for index, item in enumerate(geometry):
        item['index'] = index
        item['start'], item['end'] = item['end'], item['start']
    assert replan.progress_fingerprint(snapshot) == replan.progress_fingerprint(changed)


@pytest.mark.asyncio
async def test_profile_replan_uses_geometry_tool_and_validates_rejections():
    before, snapshot, failure = inputs()
    bad = proposal(); bad['operations'][-2]['args']['length_mm'] = 1
    provider = ProviderTurns([call('freecad_execute', bad)], [call('freecad_execute', proposal())])
    result = await FreeCADOperationGenerator(client=provider, provenance_reader=_provenance).repair(
        source_code=before.model_dump_json(), failure=failure, diagnostic=snapshot,
        profile_context=context(), base_state=None, output_formats=('step', 'stl'))
    assert result.provenance['profile_replan']['acceptance_hash'] == context()['acceptance_hash']
    assert len(provider.requests) == 2
    names = {t['function']['name'] for t in provider.requests[0]['tools']}
    assert 'freecad_execute' in names and 'freecad_patch_constraints' not in names and 'freecad_execute_api' not in names
    assert 'frames, features, exports' in json.dumps(provider.requests[1]['messages'])


@pytest.mark.asyncio
async def test_topology_error_without_backend_context_stops_before_provider():
    before, snapshot, failure = inputs(); provider = ProviderTurns()
    with pytest.raises(ValueError, match='context'):
        await FreeCADOperationGenerator(client=provider, provenance_reader=_provenance).repair(
            source_code=before.model_dump_json(), failure=failure, diagnostic=snapshot,
            base_state=None, output_formats=('step', 'stl'))
    assert not provider.requests


@pytest.mark.asyncio
async def test_profile_tool_budget_stops_rejected_proposals(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'profile_replan_max_tool_calls', 1)
    before, snapshot, failure = inputs()
    bad = proposal(); bad['operations'][-2]['args']['length_mm'] = 1
    provider = ProviderTurns([call('freecad_execute', bad)])
    with pytest.raises(ValueError, match='tool-call budget'):
        await FreeCADOperationGenerator(client=provider, provenance_reader=_provenance).repair(
            source_code=before.model_dump_json(), failure=failure, diagnostic=snapshot,
            profile_context=context(), base_state=None, output_formats=('step', 'stl'))
    assert len(provider.requests) == 1
