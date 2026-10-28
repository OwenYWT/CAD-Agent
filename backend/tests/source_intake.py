"""Controlled planning inputs for real source-intake integration tests.

Only the provider proposal is fixed. Execution, measurement, file storage,
candidate review and commit use production code. This is not an AI benchmark.
"""
from app.contracts.acceptance_v2 import AcceptanceContractV2
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.operation_generator import FreeCADOperationGenerationResult
from app.models.schemas import CADPlan, DesignBrief
from app.validation.requirement_binding import require_bound_sources
from tests.constraint_incident import IncidentRequirements, fixture_provenance


OBJECTIVE = '设计一个可夹在 25 mm 桌板上的耳机挂钩'


class SourceIntakeRequirements(IncidentRequirements):
    async def requirements_generation(self, objective, **kwargs):
        extracted = require_bound_sources(objective, requirement_sources=kwargs.get('requirement_sources'))
        contract = AcceptanceContractV2.model_validate({'objective': objective,
            'source_context': extracted['source_context'], 'checks': [
                {'check_id': r['requirement_id'], 'binding_ref': r['requirement_id'], 'kind': r['kind'],
                 'source_kind': r['source_kind'], 'source_quote': r['source_quote'], 'description': '桌板所需夹口净空',
                 'comparison': r['comparison'], 'scope': r['scope']} for r in extracted['requirements']]})
        return CADPlan(description=objective, part_type='custom', dimensions={}, features=[], constraints=[],
            design_brief=DesignBrief(intent_summary=objective, artifact_type='custom', acceptance=contract))


class SourceIntakeOperations:
    def __init__(self, gap=25):
        self.gap = gap
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        operations = []
        def add(action, **args):
            operations.append({'op_id': 'op-' + str(len(operations)), 'action': action, 'args': args})
        add('sketch.create', name='Base')
        add('sketch.add_profile', sketch='Base', geometry={'kind':'rectangle', 'corner':{'x':0,'y':0},
            'width_mm':50, 'height_mm':self.gap + 10})
        add('feature.pad', name='Pad', profile='Base', length_mm=20)
        add('sketch.create', name='Mouth', offset_mm=20)
        add('sketch.add_profile', sketch='Mouth', geometry={'kind':'rectangle', 'corner':{'x':10,'y':5},
            'width_mm':40, 'height_mm':self.gap})
        add('feature.pocket', name='MouthCut', profile='Mouth', through_all=True)
        add('document.export', objects=['Body'], formats=['fcstd','step','stl'])
        plan = FreeCADOperationPlan.model_validate({'operations': operations})
        source = plan.model_dump_json()
        return FreeCADOperationGenerationResult(operation_plan=plan, source_code=source,
            generator_kind='controlled-source-intake', provenance=fixture_provenance(source))
