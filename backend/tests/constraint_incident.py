"""Retained incident inputs; only provider proposals are controlled in chain tests.

No native execution, diagnostic, verification result or persisted state is faked.
This is a regression fixture, not a model success-rate benchmark.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from app.agent.durable_planner import DurableAgentPlanner
from app.agent.durable_plan import ValidationGatePolicy, GateMode
from app.contracts.acceptance import AcceptanceContract
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.constraint_patch import build_patch_context
from app.freecad.operation_generator import FreeCADOperationGenerationResult, FreeCADOperationGenerator
from app.models.schemas import CADPlan, DesignBrief
from tests.test_freecad_agent_tools import ProviderTurns, call

FIXTURE = json.loads((Path(__file__).parent / 'e2e/fixtures/native_failures/headphone_hook_constraints.json').read_text())


def acceptance(objective):
    # Independent test criterion. Do NOT overwrite the historical task's bad
    # measurement probes with these values in production.
    return AcceptanceContract.model_validate({'objective': objective, 'checks': [
        {'check_id': 'gap', 'kind': 'surface_clearance', 'nominal': 25.0,
         'description': '桌板夹持间隙', 'source_quote': '25 mm',
         'scope': {'centers_mm': [[15, 10, 5], [15, 10, 30]]}},
        {'check_id': 'one-solid', 'kind': 'solid_count', 'nominal': 1.0,
         'description': '单一连通实体', 'source_quote': '一个连通实体'},
    ]})


class IncidentRequirements(DurableAgentPlanner):
    async def requirements_generation(self, objective, **kwargs):
        return CADPlan(description=objective, part_type='custom', dimensions={}, features=[], constraints=[],
            modeling_hint='native sketch constraints', design_brief=DesignBrief(intent_summary=objective,
            artifact_type='custom', acceptance=acceptance(objective), open_questions=['确认此回归测试的固定要求']))

    def compose_freecad_generation(self, *args, **kwargs):
        plan = super().compose_freecad_generation(*args, **kwargs)
        off = ValidationGatePolicy(mode=GateMode.DISABLED, repair_budget=0)
        return plan.model_copy(update={'validation_policy': plan.validation_policy.model_copy(update={
            'visual': off, 'dfm': off,
            'geometry': plan.validation_policy.geometry.model_copy(update={'repair_budget': 0})})})


def fixture_provenance(source=''):
    return {'provider': 'retained-incident-fixture', 'model': 'deterministic-regression',
        'provider_response_id': 'incident-regression-proposal', 'request_hash': hashlib.sha256(b'incident').hexdigest(),
        'response_hash': hashlib.sha256(source.encode()).hexdigest(), 'finish_reason': 'stop', 'usage': {}}


class IncidentGenerator:
    def __init__(self, *, live_repair=False, connected_fixture=True):
        self.generations = 0
        self.repairs = 0
        self.live_repair = live_repair
        self.connected_fixture = connected_fixture

    async def generate(self, **kwargs):
        self.generations += 1
        raw = deepcopy(FIXTURE['checkpoint_plan'] if self.generations == 1 else FIXTURE['plan'])
        if self.generations == 2 and self.connected_fixture:
            # Separate positive design input: the original L outline crosses
            # itself at (0,-75). Traverse the outside before the inside corner.
            # These source inputs are frozen BEFORE repair, never changed by it.
            for op in raw['operations']:
                if op['action'] == 'sketch.add_geometry' and op['args']['sketch'] == 'SketchHook':
                    for point in ('start', 'end'):
                        y = op['args']['geometry'][point]['y']
                        if y in (-75, -80): op['args']['geometry'][point]['y'] = -155 - y
                if op['op_id'].startswith('constr-hook-d1-'): op['args']['value_mm'] = 80.0
                if op['op_id'].startswith('constr-hook-d5-'): op['args']['value_mm'] = 75.0
        if self.generations == 3:
            raw = {'execution_mode': 'final', 'operations': [
                {'op_id': 'export-verified-final', 'action': 'document.export',
                 'args': {'objects': ['Body'], 'formats': ['fcstd', 'step', 'stl']}}]}
        assert self.generations <= 3
        plan = FreeCADOperationPlan.model_validate(raw)
        source = plan.model_dump_json()
        return FreeCADOperationGenerationResult(operation_plan=plan, source_code=source,
            generator_kind='retained-incident-fixture', provenance=fixture_provenance(source))

    async def repair(self, **kwargs):
        self.repairs += 1
        if self.live_repair:
            return await FreeCADOperationGenerator().repair(**kwargs)
        before = FreeCADOperationPlan.model_validate_json(kwargs['source_code'])
        context = build_patch_context(before, kwargs['diagnostic'], kwargs['failure'],
            kwargs.get('base_state'), kwargs.get('prior_contract'))
        patch = {key: context[key] for key in ('plan_hash', 'checkpoint_hash', 'diagnostic_hash', 'sketch')}
        def operation(prefix):
            return next(op for op in before.operations if op.op_id.startswith(prefix))
        if kwargs['failure']['error_code'] == 'sketch_parameter_dependency_failed':
            drivers = [op for op in before.operations if op.action == 'sketch.add_constraint'
                and op.args['sketch'] == context['sketch'] and op.args['kind'] in {'distance_x', 'distance_y'}
                and op.args['value_mm'] == 0]
            assert len(drivers) == 2
            patch['changes'] = [{'action': 'replace', 'logical_id': drivers[0].op_id,
                'constraint': {'kind': 'coincident', 'first': drivers[0].args['first'], 'second': {'datum': 'origin'}}},
                {'action': 'delete', 'logical_id': drivers[1].op_id}]
        elif context['sketch'] == 'SketchClamp':
            patch['changes'] = [{'action': 'delete', 'logical_id': operation('constr-clamp-d9-').op_id}] + [
                {'action': 'add', 'logical_id': f'span-{index}', 'constraint': {'kind': 'equal',
                 'first': {'geometry_index': 1}, 'second': {'geometry_index': index}}} for index in (3, 5, 7)]
        else:
            assert context['sketch'] == 'SketchHook'
            patch['changes'] = []
            for first, second in ((1, 2), (2, 3)):
                op = operation(f'constr-hook-coinc-{first}-{second}-')
                patch['changes'].append({'action': 'replace', 'logical_id': op.op_id,
                    'constraint': {'kind': 'coincident', 'first': {'geometry_index': first, 'point_position': 2},
                                   'second': {'geometry_index': second, 'point_position': 1}}})
            patch['changes'] += [{'action': 'delete', 'logical_id': operation(f'constr-hook-d{index}-').op_id}
                                 for index in (4, 5)]
        bad = deepcopy(patch); bad['sketch'] = 'UnrelatedSketch'
        provider = ProviderTurns(
            [call('freecad_inspect_failure', {'fields': ['constraints', 'construction', 'planned_constraints', 'solver'], 'limit': 64}, 'inspect')],
            [call('freecad_patch_constraints', bad, 'bad')],
            [call('freecad_patch_constraints', patch, 'corrected')])
        return await FreeCADOperationGenerator(client=provider, provenance_reader=fixture_provenance).repair(**kwargs)
