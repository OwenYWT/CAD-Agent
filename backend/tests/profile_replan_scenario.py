"""Controlled planning proposals for real service/kernel regression, never live-LLM evidence."""
from copy import deepcopy

from app.contracts.acceptance import AcceptanceContract
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.operation_generator import FreeCADOperationGenerationResult, FreeCADOperationGenerator
from app.models.schemas import CADPlan, DesignBrief
from tests.constraint_incident import IncidentRequirements, fixture_provenance
from tests.e2e.profile_replan_fixture import failed_plan, proposal, acceptance
from tests.test_freecad_agent_tools import ProviderTurns, call


class ProfileRequirements(IncidentRequirements):
    async def requirements_generation(self, objective, **kwargs):
        return CADPlan(description=objective, part_type='custom', dimensions={}, features=[], constraints=[],
            modeling_hint='native profile regression', design_brief=DesignBrief(intent_summary=objective,
            artifact_type='custom', acceptance=AcceptanceContract.model_validate(acceptance())))


class ProfileGenerator:
    def __init__(self, scenario):
        self.scenario, self.generations, self.repairs = scenario, 0, 0

    async def generate(self, **kwargs):
        self.generations += 1
        raw = failed_plan() if self.generations == 1 else {'document_name': 'Model', 'operations': [
            {'op_id': 'export-final', 'action': 'document.export', 'args': {
                'objects': ['Body'], 'formats': ['fcstd', 'step', 'stl'], 'basename': 'model'}}]}
        plan = FreeCADOperationPlan.model_validate(raw); source = plan.model_dump_json()
        return FreeCADOperationGenerationResult(operation_plan=plan, source_code=source,
            generator_kind='controlled-profile-regression', provenance=fixture_provenance(source))

    async def repair(self, **kwargs):
        self.repairs += 1
        if self.scenario == 'budget':
            raw = deepcopy(kwargs['profile_context']['source_plan'])
            raw['operations'][2]['args']['geometry']['corner']['x'] += 1
        else:
            raw = proposal(gap=24 if self.scenario == 'wrong-gap' else 25)
        provider = ProviderTurns([call('freecad_execute', raw)])
        return await FreeCADOperationGenerator(client=provider, provenance_reader=fixture_provenance).repair(**kwargs)
