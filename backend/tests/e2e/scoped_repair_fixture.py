"""Prepare real incident geometry with a separately frozen deterministic test contract."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def prepared():
    from app.freecad.contracts import FreeCADOperationPlan
    from app.freecad.profile_replan import build_context, compile_patch, make_contract, verify_contract
    from app.freecad.failure_snapshot import digest
    root = Path(__file__).parent / 'fixtures/native_failures'
    before = FreeCADOperationPlan.model_validate_json((root/'crossed_profile_plan.json').read_text())
    snapshot = json.loads((root/'crossed_profile_diagnostic.json').read_text())
    response = json.loads((root/'crossed_profile_replan.json').read_text())
    # A new test's requirement is frozen before repair; historical acceptance is never rewritten.
    acceptance = {'objective': '25 mm gap', 'checks': [{'check_id': 'gap', 'kind': 'planar_gap',
        'nominal': 25, 'description': '25 mm planar opening', 'source_quote': '25 mm gap',
        'scope': {'axis': [0, 0, 1], 'region_min_mm': [-40, -25, -17.5],
                  'region_max_mm': [0, 0, 17.5]}}]}
    failure = {'error_code': 'profile_geometry_invalid', 'operation_id': 'pad-hook',
               'engineering_acceptance': acceptance}
    context = build_context(before, snapshot, failure)
    patch = {'plan_hash': digest(context['source_plan']), 'checkpoint_hash': context['checkpoint_hash'],
             'diagnostic_hash': digest(snapshot), 'sketch': context['sketch'],
             'operations': [op for op in response['operations'] if op['args'].get('sketch') == context['sketch']]}
    nominal = compile_patch(patch, context)
    for op in patch['operations']:
        if op['op_id'] == 'hook-dx-anchor':
            op['args'] = {'sketch': 'HookSketch', 'kind': 'point_on_object',
                'first': op['args']['first'], 'second': {'datum': 'vertical_axis'}}
    linked = compile_patch(patch, context)
    policy = {'max_tool_calls': 8, 'max_rejections': 3, 'max_parameter_probes': 128, 'perturbation_fraction': 0.01}
    result = {'original': before.model_dump(mode='json'), 'acceptance': context['acceptance'], 'patch': patch}
    for name, plan in [('independent_anchor', nominal), ('datum_anchor', linked)]:
        contract = make_contract(plan, context, policy)
        verify_contract(plan, contract, acceptance, context['checkpoint_hash'])
        result[name] = {'plan': plan.model_dump(mode='json'), 'profile_replan': contract,
                        'profile_acceptance_hash': contract['acceptance_hash']}
    return result


if __name__ == '__main__':
    content = json.dumps(prepared(), sort_keys=True, ensure_ascii=False)
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(content)
    else:
        print(content)
