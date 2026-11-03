"""Controlled input for the real native assembly workflow; no mocked gates."""
import json
from pathlib import Path

from app.domain.requirement_basis import RequirementBasisV1
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.operation_generator import FreeCADOperationGenerationResult
from tests.constraint_incident import fixture_provenance
from tests.intake_semantics import IntakeSemanticsRequirements
from tests.test_rectangular_assembly_requirements import ENVELOPE_CONFIRMATION


def assembly_case():
    case=json.loads((Path(__file__).parent/'fixtures/intake_1030_feedback.json').read_text())['cases'][0]
    return case['objective'],RequirementBasisV1(target=case['objective'],
        dimensions=case['dimensions']+'\n'+ENVELOPE_CONFIRMATION,source_kind='user_specification',
        source_reference='2026-11-01 用户明确确认装配尺寸不含安装耳；仅验收几何结构',
        design_scope='geometry',concept_acknowledged=True)


class AssemblyRequirements(IntakeSemanticsRequirements):
    def __init__(self):super().__init__('rectangular-assembly')


class AssemblyOperations:
    def __init__(self,variant='good'):
        self.variant,self.calls=variant,0

    async def generate(self,**kwargs):
        self.calls+=1
        source=f'''
variant={self.variant!r}
shape=Part.makeBox(90,60,32).cut(Part.makeBox(84,54,29,App.Vector(3,3,3)))
for x,y in [(-12,0),(-12,50),(90,0),(90,50)]:
    shape=shape.fuse(Part.makeBox(12,10,3,App.Vector(x,y,0)))
groove_depth=1.7 if variant=='wrong_groove' else 1.5
groove=Part.makeBox(89.4,59.4,groove_depth,App.Vector(.3,.3,32-groove_depth)).cut(
    Part.makeBox(84.6,54.6,groove_depth,App.Vector(2.7,2.7,32-groove_depth)))
shape=shape.cut(groove)
for index,(x,y) in enumerate([(-6,5),(-6,55),(96,5),(96,55)]):
    shape=shape.cut(Part.makeCylinder(2.3,3,App.Vector(x+(.4 if variant=='off_center_hole' and index==0 else 0),y,0)))
shape=shape.cut(Part.makeCylinder(6.25,3,App.Vector(0,30,17.5),App.Vector(1,0,0)))
if variant=='unknown_missing_material':
    shape=shape.cut(Part.makeCylinder(.01,.5,App.Vector(45,2.5,15),App.Vector(0,1,0)))
shape=shape.removeSplitter()
lid_shape=Part.makeBox(90,60,3,App.Vector(0,0,32))
for name,material in [('Body',shape),('Lid',lid_shape)]:
    body=document.addObject('PartDesign::Body',name)
    feature=body.newObject('PartDesign::Feature',name+'Material')
    feature.Shape=material
    body.Tip=feature
document.recompute()
'''
        plan=FreeCADOperationPlan.model_validate({'operations':[
            {'op_id':'assembly','action':'api.execute','args':{'source':source}},
            {'op_id':'export','action':'document.export','args':{'objects':['Body','Lid'],'formats':['fcstd','step','stl']}}]})
        encoded=plan.model_dump_json()
        return FreeCADOperationGenerationResult(operation_plan=plan,source_code=encoded,
            generator_kind='controlled-assembly-regression',provenance=fixture_provenance(encoded))
