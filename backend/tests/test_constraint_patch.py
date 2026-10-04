from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.constraint_patch import PatchRejected, build_patch_context, compile_patch

FIXTURES = Path(__file__).parent / 'e2e/fixtures/native_failures'


def context_and_patch():
    fixture = json.loads((FIXTURES / 'headphone_hook_constraints.json').read_text())
    diagnostic = json.loads((FIXTURES / 'headphone_hook_diagnostic.json').read_text())
    before = FreeCADOperationPlan.model_validate(fixture['plan'])
    failure = diagnostic['failure']
    # Test-owned independent criterion for the retained construction. These
    # probes are NOT substituted into the historical production requirement.
    failure['engineering_acceptance'] = {'objective': fixture['objective'], 'checks': [{
        'check_id': 'gap', 'kind': 'surface_clearance', 'description': '25 mm clamp gap',
        'source_quote': '25 mm', 'source_kind': 'user', 'nominal': 25.0,
        'scope': {'centers_mm': [[15, 10, 5], [15, 10, 30]]}}]}
    context = build_patch_context(before, diagnostic['snapshot'], failure, diagnostic['base_state'])
    removable = next(op.op_id for op in before.operations if op.op_id.startswith('constr-clamp-d9-'))
    patch = {key: context[key] for key in ('plan_hash', 'checkpoint_hash', 'diagnostic_hash', 'sketch')}
    patch['changes'] = [{'action': 'delete', 'logical_id': removable}] + [
        {'action': 'add', 'logical_id': f'span-{i}', 'constraint': {'kind': 'equal',
            'first': {'geometry_index': 1}, 'second': {'geometry_index': i}}} for i in (3, 5, 7)]
    return before, context, patch


def test_real_incident_patch_preserves_other_operations_and_has_a_relationship_proof():
    before, context, patch = context_and_patch()
    after, contract = compile_patch(before, patch, context)
    assert len(after.operations) == len(before.operations) + 2
    fixed = lambda plan: [op for op in plan.operations if not (op.action == 'sketch.add_constraint'
                                                    and op.args['sketch'] == 'SketchClamp')]
    assert fixed(after) == fixed(before)
    assert contract['sketches']['SketchClamp']['derivations']
    assert compile_patch(before, deepcopy(patch), context) == (after, contract)
    assert context['acceptance_hash'] == contract['acceptance_hash']


@pytest.mark.parametrize('field', ['plan_hash', 'checkpoint_hash', 'diagnostic_hash', 'sketch'])
def test_stale_baseline_and_cross_sketch_patch_rejected(field):
    before, context, patch = context_and_patch()
    patch[field] = 'OtherSketch' if field == 'sketch' else '0' * 64
    with pytest.raises(PatchRejected, match='baseline') as error:
        compile_patch(before, patch, context)
    assert error.value.differences[0]['field'] == field


def test_deleting_requested_gap_is_protected_even_when_other_dimensions_imply_it():
    before, context, patch = context_and_patch()
    gap = next(op for op in before.operations if op.op_id.startswith('constr-clamp-d4-'))
    patch['changes'] = [{'action': 'delete', 'logical_id': gap.op_id}]
    with pytest.raises(PatchRejected, match='requirement'):
        compile_patch(before, patch, context)


def test_unverified_constraint_number_and_unknown_provenance_never_grant_authority():
    before, context, patch = context_and_patch()
    patch['changes'] = [{'action': 'delete', 'logical_id': '26'}]
    with pytest.raises(PatchRejected, match='provenance'):
        compile_patch(before, patch, context)


def test_deletion_checks_expressions_before_native_constraint_renumbering():
    before, context, patch = context_and_patch()
    context['snapshot']['references'] = [{'object': 'Downstream', 'expressions': [['Length', 'SketchClamp.Constraints[25]']]}]
    with pytest.raises(PatchRejected, match='references'):
        compile_patch(before, patch, context)


def test_changed_dimension_and_blanket_locking_are_not_valid_patch_tools():
    before, context, patch = context_and_patch()
    patch['changes'][1]['constraint'] = {'kind': 'distance', 'first': {'geometry_index': 3}, 'value_mm': 41}
    with pytest.raises(PatchRejected, match='construction basis'):
        compile_patch(before, patch, context)
    patch['changes'][1]['constraint'] = {'kind': 'block', 'first': {'geometry_index': 3}}
    with pytest.raises(ValueError):
        compile_patch(before, patch, context)


def test_required_connection_cannot_be_deleted_to_make_solver_happy():
    before, context, patch = context_and_patch()
    connection = next(op for op in before.operations if op.op_id.startswith('constr-clamp-coinc-0-1-'))
    patch['changes'] = [{'action': 'delete', 'logical_id': connection.op_id}]
    with pytest.raises(PatchRejected, match='relationship'):
        compile_patch(before, patch, context)


def test_identity_only_replacement_does_not_count_as_progress():
    before, context, patch = context_and_patch()
    old = next(op for op in before.operations if op.op_id.startswith('constr-clamp-h0-'))
    patch['changes'] = [{'action': 'replace', 'logical_id': old.op_id,
                         'constraint': {k: v for k, v in old.args.items() if k != 'sketch'}}]
    with pytest.raises(PatchRejected, match='no semantic progress'):
        compile_patch(before, patch, context)


def test_committed_operation_ledger_is_immutable():
    from app.freecad.failure_snapshot import digest
    before, context, patch = context_and_patch()
    op = next(op for op in before.operations if op.op_id == patch['changes'][0]['logical_id'])
    context['snapshot']['checkpoint_operations'][op.op_id] = digest(op.model_dump(mode='json'))
    with pytest.raises(PatchRejected, match='immutable'):
        compile_patch(before, patch, context)


def test_alias_and_reference_dimension_are_protected_by_native_dependencies():
    before, context, patch = context_and_patch()
    target = next(c for s in context['snapshot']['sketches'] for c in s['constraints']
                  if c['logical_id'] == patch['changes'][0]['logical_id'])
    target['name'] = 'UserHeight'
    with pytest.raises(PatchRejected, match='alias'):
        compile_patch(before, patch, context)


def test_certificate_revalidation_rejects_tampering_and_changed_acceptance():
    from app.freecad.constraint_patch import verify_compiled_contract
    before, context, patch = context_and_patch()
    after, certificate = compile_patch(before, patch, context)
    verify_compiled_contract(after, certificate, context['acceptance'])
    altered = deepcopy(certificate)
    altered['sketches']['SketchClamp']['derivations'] = []
    with pytest.raises(PatchRejected, match='obligations'):
        verify_compiled_contract(after, altered, context['acceptance'])
    acceptance = deepcopy(context['acceptance']); acceptance['checks'][0]['nominal'] = 26
    with pytest.raises(PatchRejected, match='frozen acceptance'):
        verify_compiled_contract(after, certificate, acceptance)


def test_generic_kernel_success_cannot_replace_native_repair_verification():
    from app.freecad.constraint_patch import verify_native_receipt
    before, context, patch = context_and_patch()
    _, certificate = compile_patch(before, patch, context)
    with pytest.raises(PatchRejected, match='missing'):
        verify_native_receipt({'result': {'status': 'succeeded', 'validations': []}}, certificate)


def test_progress_fingerprint_ignores_renaming_but_tracks_physical_solver_progress():
    from app.freecad.constraint_patch import progress_fingerprint
    _, context, _ = context_and_patch()
    original = context['snapshot']
    renamed = deepcopy(original)
    for sketch in renamed['sketches']:
        for row in sketch['constraints']:
            row.update(logical_id='renamed-' + row['logical_id'], operation_id='unrelated-new-id')
    assert progress_fingerprint(original) == progress_fingerprint(renamed)
    renamed['sketches'][0]['solver']['degrees_of_freedom'] = 1
    assert progress_fingerprint(original) != progress_fingerprint(renamed)


def test_delete_then_add_equivalent_datum_is_compared_to_surviving_constraints():
    before, context, patch = context_and_patch()
    origins = [op for op in before.operations if op.action == 'sketch.add_constraint'
        and op.args['sketch'] == context['sketch']
        and op.args['kind'] in {'distance_x', 'distance_y'} and op.args['value_mm'] == 0]
    assert len(origins) == 2
    patch['changes'] += [{'action': 'delete', 'logical_id': op.op_id} for op in origins]
    patch['changes'].append({'action': 'add', 'logical_id': 'origin-datum',
        'constraint': {'kind': 'coincident', 'first': origins[0].args['first'], 'second': {'datum': 'origin'}}})
    after, certificate = compile_patch(before, patch, context)
    assert all(op not in after.operations for op in origins)
    assert len(certificate['sketches'][context['sketch']]['derivations']) == 3
