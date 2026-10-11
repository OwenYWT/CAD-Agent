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
    def __init__(self, scenario='success'):
        super().__init__()
        self.scenario = scenario

    async def requirements_generation(self, objective, **kwargs):
        from tests.e2e.scoped_repair_fixture import prepared
        frozen = prepared()['acceptance'] if self.scenario == 'crossed-incident' else acceptance()
        return CADPlan(description=objective, part_type='custom', dimensions={}, features=[], constraints=[],
            modeling_hint='native profile regression', design_brief=DesignBrief(intent_summary=objective,
            artifact_type='custom', acceptance=AcceptanceContract.model_validate(frozen)))


class ProfileGenerator:
    def __init__(self, scenario):
        self.scenario, self.generations, self.repairs = scenario, 0, 0

    async def generate(self, **kwargs):
        self.generations += 1
        raw = failed_plan() if self.generations == 1 else {'document_name': 'Model', 'operations': [
            {'op_id': 'export-final', 'action': 'document.export', 'args': {
                'objects': ['Body'], 'formats': ['fcstd', 'step', 'stl'], 'basename': 'model'}}]}
        if self.scenario == 'crossed-incident' and self.generations == 1:
            from tests.e2e.scoped_repair_fixture import prepared
            raw = prepared()['original']
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
        from app.freecad.failure_snapshot import digest
        ctx = kwargs['profile_context']
        patch = {'plan_hash': digest(ctx['source_plan']), 'checkpoint_hash': ctx['checkpoint_hash'],
                 'diagnostic_hash': digest(ctx['snapshot']), 'sketch': ctx['sketch'],
                 'operations': [op for op in raw['operations'] if op['args'].get('sketch') == ctx['sketch']]}
        if self.scenario == 'crossed-incident':
            from tests.e2e.scoped_repair_fixture import prepared
            patch['operations'] = prepared()['patch']['operations']
        provider = ProviderTurns([call('freecad_patch_profile', patch)])
        return await FreeCADOperationGenerator(client=provider, provenance_reader=fixture_provenance).repair(**kwargs)
