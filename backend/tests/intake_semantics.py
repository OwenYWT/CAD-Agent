"""Controlled provider proposals; real admission, native execution and evidence.

Saved acceptance replies replay the production incidents. The other cases are
independently checked geometric fixtures, not claims about live model quality.
"""
from copy import deepcopy
import json
from pathlib import Path

from app.agent.durable_planner import DurableAgentPlanner
from app.contracts.acceptance_v2 import AcceptanceContractV2
from app.domain.requirement_basis import RequirementBasisV1
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.operation_generator import FreeCADOperationGenerationResult
from app.validation.requirement_binding import require_bound_sources
from tests.constraint_incident import IncidentRequirements, fixture_provenance
from tests.test_freecad_agent_tools import ProviderTurns, call


RECEIPTS = json.loads((Path(__file__).parent/'fixtures/intake_1030_receipts.json').read_text())['tasks']


def intake_case(name):
    if name in {'fdm', 'context'}:
        saved = RECEIPTS[0 if name == 'fdm' else 1]
        return saved['objective'], RequirementBasisV1.model_validate(saved['requirement_basis'])
    objective = {'diameter':'生成圆柱体几何模型。圆柱体直径为40 mm，圆柱体高度为25 mm。',
        'radius':'生成圆柱体几何模型。圆柱体半径为20 mm，圆柱体高度为25 mm。',
        'passage':'板40×30×12mm，孔径6mm，孔须打通。',
        'cube':'构建一个十厘米的正方体', 'cube_rectangular':'构建一个十厘米的正方体',
        'cube_split':'构建一个正方体', 'chinese_box':'长方体长100mm宽二十厘米高三十厘米',
        'sphere_bounds':'球体长40mm宽40mm高40mm', 'cylinder_bounds':'圆柱体长40mm宽40mm厚25mm',
        'wall':'盒体外壁厚度：3 mm', 'floor':'盒体底板厚度：3 mm'}[name]
    basis = RequirementBasisV1(target=objective, dimensions='边长两点五厘米' if name == 'cube_split' else objective, source_kind='user_specification',
        source_reference='用户指定的设计尺寸', design_scope='geometry')
    return objective, basis


class IntakeSemanticsRequirements(IncidentRequirements):
    def __init__(self, name):
        super().__init__()
        self.name = name
        self.provider = None

    async def requirements_generation(self, objective, **kwargs):
        if self.name in {'fdm','context'}:
            saved = RECEIPTS[0 if self.name == 'fdm' else 1]
            proposal = deepcopy(saved['calls'][-1]['acceptance'])
        else:
            extracted = require_bound_sources(objective, requirement_sources=kwargs.get('requirement_sources'))
            proposal = AcceptanceContractV2.model_validate({'objective':objective, 'checks':[
                {'check_id':r['requirement_id'],'binding_ref':r['requirement_id'], 'kind':r['kind'],
                 'source_kind':r['source_kind'], 'source_quote':r['source_quote'],'description':r['property'],
                 'comparison':r['comparison'], 'scope':r['scope']} for r in extracted['requirements']]}).model_dump(mode='json')
        self.provider = ProviderTurns([call('submit_cad_plan', {'description':objective, 'part_type':'bracket',
            'dimensions':{}, 'features':[], 'constraints':[], 'design_brief':{'intent_summary':objective,
            'artifact_type':'custom', 'acceptance':proposal}})])
        self.planner._client = self.provider
        return await DurableAgentPlanner.requirements_generation(self, objective, **kwargs)


class IntakeSemanticsOperations:
    def __init__(self, name, valid=True):
        self.name, self.valid, self.calls = name, valid, 0

    async def generate(self, **kwargs):
        self.calls += 1
        operations = []
        def add(action, **args):
            operations.append({'op_id':'op-'+str(len(operations)), 'action':action, 'args':args})
        add('sketch.create', name='Base')
        if self.name in {'sphere_bounds', 'cylinder_bounds'}:
            shape = ('Part.makeSphere(20)' if self.name == 'sphere_bounds' else 'Part.makeCylinder(20,25)') if self.valid else (
                'Part.makeBox(40,40,40)' if self.name == 'sphere_bounds' else 'Part.makeBox(40,40,25)')
            operations.clear()
            add('api.execute', source="body=document.addObject('PartDesign::Body','Body')\n"
                "feature=body.newObject('PartDesign::Feature','Primitive')\n"
                f"feature.Shape={shape}\nbody.Tip=feature\n")
        elif self.name in {'cube', 'cube_split', 'cube_rectangular', 'chinese_box'}:
            length, width, height = ((25, 25, 25) if self.name == 'cube_split' else
                                    (100, 200, 300) if self.name == 'chinese_box' else (100, 100, 100))
            if not self.valid:
                if self.name == 'cube_rectangular':
                    width = 50
                elif self.name == 'chinese_box':
                    length += 1
                else:
                    length, width, height = length+1, width+1, height+1
            add('sketch.add_profile', sketch='Base', geometry={'kind':'rectangle', 'corner':{'x':0,'y':0},
                'width_mm':length, 'height_mm':width})
            add('feature.pad', name='Pad', profile='Base', length_mm=height)
        elif self.name in {'diameter','radius'}:
            add('sketch.add_profile', sketch='Base', geometry={'kind':'circle', 'center':{'x':0,'y':0},
                'radius_mm':20 if self.valid else 21})
            add('feature.pad', name='Pad', profile='Base', length_mm=25)
        elif self.name == 'passage':
            add('sketch.add_profile', sketch='Base', geometry={'kind':'rectangle', 'corner':{'x':0,'y':0},
                'width_mm':40, 'height_mm':30})
            add('feature.pad', name='Pad', profile='Base', length_mm=12)
            add('sketch.create', name='Bore', offset_mm=12)
            add('sketch.add_profile', sketch='Bore', geometry={'kind':'circle',
                'center':{'x':20,'y':15}, 'radius_mm':3})
            add('feature.pocket', name='BoreCut', profile='Bore', length_mm=12 if self.valid else 6)
        else:
            height = 3 if self.name == 'wall' and not self.valid else 35
            add('sketch.add_profile', sketch='Base', geometry={'kind':'rectangle', 'corner':{'x':0,'y':0},
                'width_mm':90 if self.valid or self.name in {'wall','floor'} else 89, 'height_mm':60})
            add('feature.pad', name='Pad', profile='Base', length_mm=height)
            if self.name in {'fdm','wall','floor'}:
                wall = 1 if self.name == 'wall' and not self.valid else 3
                floor = 1 if not self.valid else 3
                add('sketch.create', name='Cavity', offset_mm=height)
                add('sketch.add_profile', sketch='Cavity', geometry={'kind':'rectangle',
                    'corner':{'x':wall,'y':wall},'width_mm':90-2*wall,'height_mm':60-2*wall})
                add('feature.pocket', name='CavityCut', profile='Cavity', length_mm=height-floor)
        add('document.export', objects=['Body'], formats=['fcstd','step','stl'])
        plan = FreeCADOperationPlan.model_validate({'operations':operations})
        source = plan.model_dump_json()
        return FreeCADOperationGenerationResult(operation_plan=plan, source_code=source,
            generator_kind='controlled-intake-semantics', provenance=fixture_provenance(source))
