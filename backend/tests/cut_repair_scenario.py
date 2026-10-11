"""Two real no-effect cuts with deterministic provider proposals, not simulated CAD."""
from app.contracts.acceptance import AcceptanceContract
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.failure_snapshot import digest
from app.freecad.operation_generator import FreeCADOperationGenerationResult, FreeCADOperationGenerator
from app.models.schemas import CADPlan, DesignBrief
from tests.constraint_incident import IncidentRequirements, fixture_provenance
from tests.test_freecad_agent_tools import ProviderTurns, call


def acceptance():
    objective = 'two blind holes, diameter 4 mm, depth 3 mm, one solid'
    return {'objective': objective, 'checks': [
        {'check_id': identity, 'kind': kind, 'nominal': value, 'description': quote, 'source_quote': quote}
        for identity, kind, value, quote in [
            ('count', 'hole_count', 2, 'two blind holes'), ('diameter', 'hole_diameter', 4, 'diameter 4 mm'),
            ('depth', 'hole_depth', 3, 'depth 3 mm'), ('connected', 'solid_count', 1, 'one solid')]]}


def failed_plan():
    operations = []
    def add(identity, action, **args):
        operations.append({'op_id': identity, 'action': action, 'args': args})
    for name, x, radius, z in [('Base', 0, 10, 0), ('First', -4, 2, 10), ('Second', 4, 2, 10)]:
        add('create-' + name.lower(), 'sketch.create', name=name, offset_mm=z)
        add('profile-' + name.lower(), 'sketch.add_profile', sketch=name,
            geometry={'kind': 'circle', 'center': {'x': x, 'y': 0}, 'radius_mm': radius})
        if name == 'Base':
            add('pad', 'feature.pad', name='Pad', profile=name, length_mm=10)
        else:
            add('cut-' + name.lower(), 'feature.pocket', name='Pocket' + name, profile=name,
                length_mm=3, reversed=True)
    add('export', 'document.export', objects=['Body'], formats=['fcstd'])
    return {'execution_mode': 'checkpoint', 'operations': operations}


class CutRequirements(IncidentRequirements):
    async def requirements_generation(self, objective, **kwargs):
        return CADPlan(description=objective, part_type='custom', dimensions={}, features=[], constraints=[],
            modeling_hint='native cut regression', design_brief=DesignBrief(intent_summary=objective,
                artifact_type='custom', acceptance=AcceptanceContract.model_validate(acceptance())))


class CutGenerator:
    def __init__(self):
        self.generations = 0
        self.repairs = []

    async def generate(self, **kwargs):
        self.generations += 1
        raw = failed_plan() if self.generations <= 2 else {'operations': [
            {'op_id': 'export-final', 'action': 'document.export', 'args': {
                'objects': ['Body'], 'formats': ['fcstd', 'step', 'stl']}}]}
        if self.generations == 1:
            raw['operations'] = [*raw['operations'][:3], raw['operations'][-1]]
        elif self.generations == 2:
            raw['operations'] = raw['operations'][3:]
        plan = FreeCADOperationPlan.model_validate(raw)
        source = plan.model_dump_json()
        return FreeCADOperationGenerationResult(operation_plan=plan, source_code=source,
            generator_kind='controlled-cut-regression', provenance=fixture_provenance(source))

    async def repair(self, **kwargs):
        before = FreeCADOperationPlan.model_validate_json(kwargs['source_code'])
        identity = kwargs['failure']['operation_id']
        patch = {'plan_hash': digest(before.model_dump(mode='json')), 'operation_id': identity, 'reversed': False}
        # Exercise persisted rejection feedback too: dimensions cannot enter a local patch.
        provider = ProviderTurns([call('freecad_patch_cut', {**patch, 'length_mm': 1})],
                                 [call('freecad_patch_cut', patch)])
        result = await FreeCADOperationGenerator(client=provider, provenance_reader=fixture_provenance).repair(**kwargs)
        changes = [(a, b) for a, b in zip(before.operations, result.operation_plan.operations) if a != b]
        assert len(changes) == 1 and changes[0][0].op_id == identity
        assert changes[0][0].args['length_mm'] == changes[0][1].args['length_mm'] == 3
        self.repairs.append(identity)
        assert len(provider.requests) == 2
        return result
