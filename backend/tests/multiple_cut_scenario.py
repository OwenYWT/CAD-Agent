"""Retained failing plan; only provider proposals are controlled, CAD stays real."""
from copy import deepcopy
import json
from pathlib import Path

from app.contracts.acceptance import AcceptanceContract
from app.freecad.constraint_patch import build_patch_context
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.failure_snapshot import digest
from app.freecad.operation_generator import FreeCADOperationGenerationResult, FreeCADOperationGenerator
from app.models.schemas import CADPlan, DesignBrief
from tests.constraint_incident import IncidentRequirements, fixture_provenance
from tests.test_freecad_agent_tools import ProviderTurns, call

FIXTURE = json.loads((Path(__file__).parent / 'e2e/fixtures/native_failures/multiple_cut_coordinates.json').read_text())


class MultipleCutRequirements(IncidentRequirements):
    def __init__(self, *, geometry_failure=False):
        super().__init__()
        self.geometry_failure = geometry_failure

    def compose_freecad_generation(self, *args, **kwargs):
        plan = super().compose_freecad_generation(*args, **kwargs)
        return plan.model_copy(update={'validation_policy':plan.validation_policy.model_copy(update={
            'geometry':plan.validation_policy.geometry.model_copy(update={'repair_budget':int(self.geometry_failure)})})})

    async def requirements_generation(self, objective, **kwargs):
        return CADPlan(description=objective, part_type='custom', dimensions={}, features=[], constraints=[],
            modeling_hint='retained multiple-cut regression', design_brief=DesignBrief(intent_summary=objective,
                artifact_type='custom', acceptance=AcceptanceContract.model_validate(FIXTURE['acceptance'])))


class MultipleCutGenerator:
    def __init__(self, *, geometry_failure=False):
        self.generations = 0
        self.repairs = []
        self.geometry_failure = geometry_failure
        self.geometry_repairs = 0

    async def generate(self, **kwargs):
        self.generations += 1
        assert self.generations <= 2
        raw = deepcopy(FIXTURE['plan']) if self.generations == 1 else {'operations': [
            {'op_id': 'export-final', 'action': 'document.export', 'args': {
                'objects': ['HeadphoneHook'], 'formats': ['fcstd', 'step', 'stl']}}]}
        if self.generations == 2 and self.geometry_failure:
            raw['operations'] = [
                {'op_id':'extra-sketch','action':'sketch.create','args':{'name':'ExtraSketch','body':'ExtraBody'}},
                {'op_id':'extra-profile','action':'sketch.add_profile','args':{'sketch':'ExtraSketch',
                    'geometry':{'kind':'rectangle','corner':{'x':500,'y':500},'width_mm':2,'height_mm':2}}},
                {'op_id':'extra-pad','action':'feature.pad','args':{'name':'ExtraPad','profile':'ExtraSketch','length_mm':2}},
                {**raw['operations'][-1], 'args':{**raw['operations'][-1]['args'],'objects':['HeadphoneHook','ExtraBody']}}]
        plan = FreeCADOperationPlan.model_validate(raw)
        source = plan.model_dump_json()
        return FreeCADOperationGenerationResult(operation_plan=plan, source_code=source,
            generator_kind='retained-multiple-cut-regression', provenance=fixture_provenance(source))

    async def repair(self, **kwargs):
        before = FreeCADOperationPlan.model_validate_json(kwargs['source_code'])
        failure = kwargs['failure']
        if failure['error_code'] == 'geometry_validation_failed':
            self.geometry_repairs += 1
            # Remove the unintended operations from the final turn. The real
            # pre-turn checkpoint is reloaded; nothing is hidden from exports.
            raw = {'operations':[{'op_id':'export-final','action':'document.export','args':{
                'objects':['HeadphoneHook'],'formats':['fcstd','step','stl']}}]}
            plan = FreeCADOperationPlan.model_validate(raw)
            source = plan.model_dump_json()
            return FreeCADOperationGenerationResult(operation_plan=plan, source_code=source,
                generator_kind='controlled-geometry-correction', provenance=fixture_provenance(source))
        identity = failure['operation_id']
        self.repairs.append((identity, failure['error_code']))
        if failure['error_code'].startswith('sketch_'):
            context = build_patch_context(before, kwargs['diagnostic'], failure,
                kwargs.get('base_state'), kwargs.get('prior_contract'))
            patch = {key: context[key] for key in ('plan_hash', 'checkpoint_hash', 'diagnostic_hash', 'sketch')}
            assert context['sketch'] == 'TipUpturn'
            first = next(op for op in before.operations if op.op_id == 'line-tip-1').args['geometry']['start']
            patch['changes'] = [{'action': 'add', 'logical_id': 'anchor-' + axis,
                'constraint': {'kind': 'distance_' + axis, 'first': {'geometry_index': 0, 'point_position': 1},
                    'value_mm': first[axis]}} for axis in ('x', 'y')]
            turns = [[call('freecad_inspect_failure', {'fields': [
                'constraints', 'construction', 'planned_constraints', 'solver'], 'limit': 64})],
                [call('freecad_patch_constraints', patch)]]
        else:
            assert failure['error_code'] == 'subtractive_feature_no_effect'
            diagnostic = json.loads(failure['details']['cut_diagnostic_json'])
            patch = {'plan_hash': digest(before.model_dump(mode='json')), 'operation_id': identity, 'reversed': True}
            if identity in ('pocket-throat', 'pocket-clamp'):
                assert diagnostic['classification'] == 'wrong_direction'
            else:
                assert identity in ('pocket-screw', 'pocket-tip')
                assert diagnostic['classification'] == 'profile_outside_material'
                sketch = next(op for op in before.operations if op.op_id == identity).args['profile']
                create = next(op for op in before.operations if op.action == 'sketch.create' and op.args['name'] == sketch)
                patch['placement'] = {key: deepcopy(create.args[key]) for key in ('plane', 'offset_mm', 'reversed', 'frame')}
                patch['placement']['frame']['normal'] = [0, -1, 0]
                patch['reversed'] = identity == 'pocket-screw'
            turns = [[call('freecad_patch_cut', patch)]]
        return await FreeCADOperationGenerator(client=ProviderTurns(*turns),
            provenance_reader=fixture_provenance).repair(**kwargs)
