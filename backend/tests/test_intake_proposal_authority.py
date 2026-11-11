"""Replay saved real planning replies at the unfrozen proposal boundary."""
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.planner import _planning_format, _v2_requirements_prompt
from app.contracts.acceptance_v2 import AcceptanceContractV2, RequirementBindingError
from app.domain.requirement_basis import RequirementBasisV1
from app.models.schemas import CADPlan, ModificationPlan
from app.validation import requirement_binding as binding
from app.workflows.handlers.planning import requirements_objective, requirements_sources


RECEIPTS = json.loads((Path(__file__).parent / 'fixtures/intake_1030_receipts.json').read_text())['tasks']


def test_source_parser_gaps_are_not_presented_as_measurement_capability_limits():
    objective = 'Create three coaxial cylindrical segments and a centered axial through bore of diameter 8 mm.'
    sources = [{'source_input_id': 'objective', 'source_kind': 'user', 'text': objective},
               {'source_input_id': 'dimensions', 'source_kind': 'confirmed', 'text': 'Segment heights: 10, 20, 30 mm.'}]
    objective += '\n' + sources[1]['text']
    parsed = binding.extract_source_requirements(objective, requirement_sources=sources)
    assert parsed['unsupported']  # A parser gap exists; the model still receives the full request.
    prompt = _v2_requirements_prompt(objective, 'submit_cad_plan', requirement_sources=sources)
    context = json.loads(prompt.split('Saved requirement sources and quotation references:\n', 1)[1])
    assert context['sources'] == sources
    assert 'unsupported' not in context and 'obligations' not in context
    assert all(set(r) == {'binding_ref', 'source_input_id', 'source_kind', 'source_quote'}
               for r in context['references'])
    assert {r['binding_ref'] for r in context['references']} == {
        r.get('requirement_id') or r['obligation_id']
        for r in [*parsed['requirements'], *parsed['obligations']]}
    assert all(r['source_kind'] == next(s['source_kind'] for s in sources
        if s['source_input_id'] == r['source_input_id']) for r in context['references'])
    assert 'hole_through' in prompt


def replay_inputs(task):
    request = SimpleNamespace(objective=task['objective'], output_formats=task['output_formats'],
        operation_context=SimpleNamespace(requirement_basis=RequirementBasisV1.model_validate(task['requirement_basis'])))
    return requirements_objective(request), requirements_sources(request)


@pytest.mark.parametrize('task', RECEIPTS, ids=lambda t: t['task_id'][:8])
def test_saved_provider_context_cannot_create_or_change_server_authority(task):
    objective, sources = replay_inputs(task)
    for call in task['calls']:
        proposal = deepcopy(call['acceptance'])
        proposal['objective'] = objective
        binding.bind_acceptance_proposal(objective, proposal, requirement_sources=sources)
        contract = AcceptanceContractV2.model_validate(proposal)
        assert contract.source_context is None
        assert binding.validate_requirement_bindings(objective, contract, requirement_sources=sources)['status'] == 'bound'
        assert not proposal['verification_limits']
        # Original checks survive admission; only source-owned metadata changes.
        assert [c['comparison'] for c in proposal['checks']] == [c['comparison'] for c in call['acceptance']['checks']]


@pytest.mark.parametrize('field', ['quantity', 'scope', 'source_kind', 'missing_check'])
def test_server_metadata_binding_never_repairs_or_waives_measurement_requirements(field):
    task = RECEIPTS[-1]
    objective, sources = replay_inputs(task)
    proposal = deepcopy(task['calls'][-1]['acceptance'])
    proposal['objective'] = objective
    if field == 'quantity':
        proposal['checks'][0]['comparison']['value'] = 9
    elif field == 'scope':
        proposal['checks'][0]['scope']['axis'] = [0, 0, 1]
    elif field == 'source_kind':
        proposal['checks'][0]['source_kind'] = 'assumption'
        proposal['checks'][0]['required'] = False
    else:
        proposal['checks'].pop()
    binding.bind_acceptance_proposal(objective, proposal, requirement_sources=sources)
    with pytest.raises(RequirementBindingError):
        binding.validate_requirement_bindings(objective, AcceptanceContractV2.model_validate(proposal), requirement_sources=sources)


@pytest.mark.parametrize('model,name', [(CADPlan, 'submit_cad_plan'), (ModificationPlan, 'submit_modification_plan')])
def test_new_tool_proposals_do_not_request_server_owned_context(model, name):
    schema = _planning_format(model, name, True, 'engineering-acceptance.v2')['tools'][0]['function']['parameters']
    contract = schema['$defs']['AcceptanceContractV2']
    assert 'source_context' not in contract['properties']
    assert contract['additionalProperties'] is False


def test_frozen_context_still_rejects_a_different_source_even_if_proposal_admission_exists():
    task = RECEIPTS[-1]
    objective, sources = replay_inputs(task)
    frozen = deepcopy(task['calls'][-1]['acceptance'])
    frozen['objective'] = objective
    with pytest.raises(RequirementBindingError, match='source context'):
        binding.validate_requirement_bindings(objective, AcceptanceContractV2.model_validate(frozen), requirement_sources=sources)
